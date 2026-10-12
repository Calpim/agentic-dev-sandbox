"""Interactive Workflow Runner for Standalone and Antigravity Chat Execution.

This module allows running the AI-driven development pipeline without requiring an external
Temporal daemon, supporting interactive human gates (Gate 1 Example Map, Gate 2 Schema,
Lightweight HIL Schema Change / RESYNC) via callbacks or terminal prompts.
"""

import asyncio
import logging
from pathlib import Path
from typing import Any, Awaitable, Callable, Dict, Optional, Tuple

from rich.console import Console
from rich.panel import Panel
from rich.prompt import Prompt

from orchestrator.activities import (
    decompose_specs_activity,
    draft_schema_activity,
    generate_example_map_activity,
    initialize_tasks_activity,
    integration_and_pr_activity,
    run_design_activity,
    run_tdd_cycle_activity,
)
from orchestrator.guards.validators import validate_pbi_id

logger = logging.getLogger("orchestrator.interactive")
console = Console()
BASE_DIR = Path(__file__).resolve().parent.parent

# Callback types:
# Returns (approved: bool, feedback: Optional[str])
GateCallback = Callable[[str, str], Awaitable[Tuple[bool, Optional[str]]]]
HilCallback = Callable[[Dict[str, Any]], Awaitable[Tuple[bool, Optional[str]]]]
ProgressCallback = Callable[[str, str], Awaitable[None]]


async def default_console_example_map_gate(path: str, content: str) -> Tuple[bool, Optional[str]]:
    """Default interactive terminal prompt for Gate 1 (Example Map)."""
    console.print(Panel(content[:1500] + ("\n..." if len(content) > 1500 else ""), title=f"Example Map: {path}"))
    choice = Prompt.ask("Gate 1: Example Map 判定", choices=["approve", "reject", "abort"], default="approve")
    if choice == "approve":
        return True, None
    elif choice == "reject":
        fb = Prompt.ask("修正指示 (Feedback) を入力してください")
        return False, fb
    else:
        raise KeyboardInterrupt("User aborted workflow at Gate 1")


async def default_console_schema_gate(path: str, content: str) -> Tuple[bool, Optional[str]]:
    """Default interactive terminal prompt for Gate 2 (OpenAPI Schema)."""
    console.print(Panel(content[:1500] + ("\n..." if len(content) > 1500 else ""), title=f"OpenAPI Schema: {path}"))
    choice = Prompt.ask("Gate 2: OpenAPI Schema 判定", choices=["approve", "reject", "abort"], default="approve")
    if choice == "approve":
        return True, None
    elif choice == "reject":
        fb = Prompt.ask("修正指示 (Feedback) を入力してください")
        return False, fb
    else:
        raise KeyboardInterrupt("User aborted workflow at Gate 2")


async def default_console_hil_gate(directive: Dict[str, Any]) -> Tuple[bool, Optional[str]]:
    """Default interactive terminal prompt for Lightweight HIL (Schema Change)."""
    msg = (
        f"[bold yellow]Directive:[/bold yellow] {directive.get('directive')}\n"
        f"[bold yellow]Reason:[/bold yellow] {directive.get('reason')}\n"
        f"[bold yellow]Instruction:[/bold yellow] {directive.get('instruction')}\n"
    )
    console.print(Panel(msg, title="Lightweight HIL: Schema Change Requested by Analyzer"))
    choice = Prompt.ask(
        "Lightweight HIL: スキーマ変更を承認して RESYNC しますか？", choices=["approve", "reject"], default="approve"
    )
    if choice == "approve":
        fb = Prompt.ask("追加のフィードバック（空欄可）", default="")
        return True, fb if fb else None
    return False, None


class InteractiveWorkflowRunner:
    """End-to-end interactive runner for PBI development workflow."""

    def __init__(
        self,
        pbi_id: str,
        pbi_content: str,
        on_gate_1: Optional[GateCallback] = None,
        on_gate_2: Optional[GateCallback] = None,
        on_lightweight_hil: Optional[HilCallback] = None,
        on_progress: Optional[ProgressCallback] = None,
    ):
        self.pbi_id = validate_pbi_id(pbi_id)
        self.pbi_content = pbi_content
        self.on_gate_1 = on_gate_1 or default_console_example_map_gate
        self.on_gate_2 = on_gate_2 or default_console_schema_gate
        self.on_lightweight_hil = on_lightweight_hil or default_console_hil_gate
        self.on_progress = on_progress
        self.stage = "INITIALIZED"

    async def _notify(self, stage: str, message: str) -> None:
        self.stage = stage
        logger.info(f"[{stage}] {message}")
        if self.on_progress:
            await self.on_progress(stage, message)

    async def run(self) -> Dict[str, Any]:
        """Runs the entire pipeline with interactive gates."""
        pbi_id = self.pbi_id
        pbi_content = self.pbi_content

        # -------------------------------------------------------------
        # Phase 1-1: Example Mapping & Gate 1
        # -------------------------------------------------------------
        await self._notify("PHASE_1_EXAMPLE_MAPPING", f"Generating Example Map for {pbi_id}...")
        example_map_feedback: Optional[str] = None
        example_map_path = str(BASE_DIR / "specs" / "example-maps" / f"{pbi_id}.md")

        while True:
            example_map_path = await generate_example_map_activity(pbi_id, pbi_content, example_map_feedback)
            content = Path(example_map_path).read_text(encoding="utf-8") if Path(example_map_path).exists() else ""

            await self._notify("WAITING_HUMAN_GATE_1_EXAMPLE_MAP", "Waiting for human approval on Example Map...")
            approved, feedback = await self.on_gate_1(example_map_path, content)
            if approved:
                await self._notify("HUMAN_GATE_1_APPROVED", "Example Map approved by human.")
                break
            else:
                await self._notify(
                    "RETRYING_EXAMPLE_MAP", f"Feedback received: {feedback}. Regenerating Example Map..."
                )
                example_map_feedback = feedback

        # -------------------------------------------------------------
        # Phase 1-2 & 1-3: Specification Decomposition
        # -------------------------------------------------------------
        await self._notify(
            "PHASE_1_GHERKIN_DECOMPOSITION", "Decomposing Example Map into Business Gherkin and Component Specs..."
        )
        decomp_result = await decompose_specs_activity(pbi_id, example_map_path)

        # -------------------------------------------------------------
        # Phase 2: Schema First (OpenAPI) & Gate 2
        # -------------------------------------------------------------
        await self._notify("PHASE_2_SCHEMA_DRAFT", "Drafting initial OpenAPI 3.1 specification...")
        schema_feedback: Optional[str] = None
        schema_path = str(BASE_DIR / "specs" / "api" / "v1" / f"{pbi_id}.yaml")

        while True:
            schema_path = await draft_schema_activity(pbi_id, decomp_result, schema_feedback)
            content = Path(schema_path).read_text(encoding="utf-8") if Path(schema_path).exists() else ""

            await self._notify("WAITING_HUMAN_GATE_2_SCHEMA", "Waiting for human approval on OpenAPI schema...")
            approved, feedback = await self.on_gate_2(schema_path, content)
            if approved:
                await self._notify("HUMAN_GATE_2_APPROVED", "OpenAPI schema approved by human.")
                break
            else:
                await self._notify("RETRYING_SCHEMA", f"Feedback received: {feedback}. Regenerating OpenAPI schema...")
                schema_feedback = feedback

        # Phase 2-2: Initialize tasks and mock server contracts
        await self._notify("PHASE_2_INITIALIZE_TASKS", "Generating Prism mock servers and task state...")
        await initialize_tasks_activity(pbi_id, schema_path)

        # -------------------------------------------------------------
        # Phase 3 & 4: App / BFF Parallel Pipelines & Lightweight HIL RESYNC
        # -------------------------------------------------------------
        max_resync_attempts = 2
        app_directive: Optional[Dict[str, Any]] = None
        bff_directive: Optional[Dict[str, Any]] = None

        app_res: Dict[str, Any] = {}
        bff_res: Dict[str, Any] = {}

        for attempt in range(max_resync_attempts + 1):
            await self._notify(
                "PHASE_3_4_PARALLEL_EXECUTION", f"Running App and BFF parallel pipelines (Attempt {attempt + 1})..."
            )

            async def _run_component(comp: str, directive: Optional[Dict[str, Any]]) -> Dict[str, Any]:
                try:
                    dir_inst = directive.get("instruction") if directive else None
                    des_res = await run_design_activity(pbi_id, comp, dir_inst)
                    return await run_tdd_cycle_activity(pbi_id, comp, des_res, directive)
                except Exception as ex:
                    logger.error(f"Error in {comp} pipeline: {ex}", exc_info=True)
                    return {"status": "FAILED", "component": comp, "error": str(ex)}

            app_task = asyncio.create_task(_run_component("app", app_directive))
            bff_task = asyncio.create_task(_run_component("bff", bff_directive))

            app_res, bff_res = await asyncio.gather(app_task, bff_task)

            app_needs_change = app_res.get("status") == "SCHEMA_CHANGE_REQUESTED"
            bff_needs_change = bff_res.get("status") == "SCHEMA_CHANGE_REQUESTED"

            if app_needs_change or bff_needs_change:
                raw_directive = app_res.get("directive") if app_needs_change else bff_res.get("directive")
                change_directive: Dict[str, Any] = raw_directive if isinstance(raw_directive, dict) else {}
                change_instruction = change_directive.get("instruction", "Schema change requested by Analyzer")

                await self._notify(
                    "WAITING_LIGHTWEIGHT_HIL_SCHEMA_CHANGE", "Lightweight HIL: Analyzer requested schema change."
                )
                approved, hil_feedback = await self.on_lightweight_hil(change_directive)

                if approved:
                    await self._notify(
                        "APPLYING_SCHEMA_RESYNC", "Schema change approved. Updating OpenAPI and mock contracts..."
                    )
                    feedback_str = hil_feedback or change_instruction
                    schema_path = await draft_schema_activity(pbi_id, decomp_result, feedback_str)
                    await initialize_tasks_activity(pbi_id, schema_path)

                    resync_dir = {
                        "directive": "RESYNC",
                        "instruction": f"OpenAPI schema has been updated and approved (attempt {attempt + 1}). Resync models and resume.",
                        "reason": change_directive.get("reason", "Schema modified via lightweight HIL"),
                    }
                    app_directive = resync_dir
                    bff_directive = resync_dir
                    continue
                else:
                    await self._notify("SCHEMA_CHANGE_REJECTED", "Schema change rejected by human. Aborting pipeline.")
                    return {
                        "pbi_id": pbi_id,
                        "status": "FAILED",
                        "stage": "SCHEMA_CHANGE_REJECTED",
                        "reason": "Schema change rejected during lightweight HIL",
                        "app_result": app_res,
                        "bff_result": bff_res,
                    }

            # If no schema change was requested, break out of resync attempt loop
            break

        # Check if pipelines succeeded
        app_success = app_res.get("status") in ("SUCCESS", "ALL_GREEN")
        bff_success = bff_res.get("status") in ("SUCCESS", "ALL_GREEN")

        if not (app_success and bff_success):
            await self._notify(
                "PIPELINE_FAILED",
                f"Component pipelines failed: app={app_res.get('status')}, bff={bff_res.get('status')}",
            )
            return {
                "pbi_id": pbi_id,
                "status": "FAILED",
                "stage": "COMPONENT_PIPELINE_FAILED",
                "app_result": app_res,
                "bff_result": bff_res,
            }

        # -------------------------------------------------------------
        # Phase 6 & 7: Integration Gate & PR Creation
        # -------------------------------------------------------------
        await self._notify(
            "PHASE_6_7_INTEGRATION_AND_PR", "Running integration verification and preparing GitHub PR..."
        )
        final_result = await integration_and_pr_activity(pbi_id, app_res, bff_res)

        if final_result.get("status") == "COMPLETED":
            await self._notify("COMPLETED", f"Workflow completed successfully! PR: {final_result.get('pr_url')}")
        else:
            await self._notify("INTEGRATION_GATE_FAILED", "Integration gate failed.")

        return final_result


async def run_single_step(
    step_name: str,
    pbi_id: str,
    pbi_content: str = "",
    feedback: Optional[str] = None,
    extra_args: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Helper for executing single pipeline steps from scripts or agents."""
    pbi_id = validate_pbi_id(pbi_id)
    extra_args = extra_args or {}

    if step_name == "example_map":
        path = await generate_example_map_activity(pbi_id, pbi_content, feedback)
        return {
            "step": step_name,
            "path": path,
            "content": Path(path).read_text(encoding="utf-8") if Path(path).exists() else "",
        }

    elif step_name == "decompose":
        map_path = extra_args.get("example_map_path", str(BASE_DIR / "specs" / "example-maps" / f"{pbi_id}.md"))
        res = await decompose_specs_activity(pbi_id, map_path)
        return {"step": step_name, "decomposition": res}

    elif step_name == "draft_schema":
        decomp = extra_args.get("decomp_result")
        if not decomp:
            decomp = {
                "business": str(BASE_DIR / "specs" / "gherkin" / "business" / f"{pbi_id}.feature"),
                "app": str(BASE_DIR / "specs" / "gherkin" / "app" / f"{pbi_id}.feature"),
                "bff": str(BASE_DIR / "specs" / "gherkin" / "bff" / f"{pbi_id}.feature"),
            }
        path = await draft_schema_activity(pbi_id, decomp, feedback)
        return {
            "step": step_name,
            "path": path,
            "content": Path(path).read_text(encoding="utf-8") if Path(path).exists() else "",
        }

    elif step_name == "initialize_tasks":
        schema_path = extra_args.get("schema_path", str(BASE_DIR / "specs" / "api" / "v1" / f"{pbi_id}.yaml"))
        await initialize_tasks_activity(pbi_id, schema_path)
        return {"step": step_name, "status": "INITIALIZED"}

    elif step_name == "integration_and_pr":
        app_res = extra_args.get("app_res", {"status": "SUCCESS"})
        bff_res = extra_args.get("bff_res", {"status": "SUCCESS"})
        res = await integration_and_pr_activity(pbi_id, app_res, bff_res)
        return {"step": step_name, "result": res}

    else:
        raise ValueError(f"Unknown step_name: {step_name}")
