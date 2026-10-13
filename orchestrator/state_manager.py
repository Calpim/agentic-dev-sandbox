"""PBI State and Directive Manager for File-Driven Agent Pipeline.

Provides clean, safe, and thread-safe APIs to manage task states, plans,
and directives without requiring Temporal or an external database.
"""

from datetime import datetime, timezone
import json
import logging
from pathlib import Path
from typing import Any, Dict, Optional

from orchestrator.guards.task_state import (
    get_task_state,
    update_pipeline_phase_atomic,
    update_task_state,
)
from orchestrator.guards.validators import validate_pbi_id, validate_safe_path

logger = logging.getLogger("orchestrator.state_manager")
BASE_DIR = Path(__file__).resolve().parent.parent


class StateManager:
    """Manages file-driven task state under tasks/{pbi_id}/."""

    def __init__(self, base_dir: Optional[Path] = None):
        self.base_dir = base_dir or BASE_DIR

    def get_task_dir(self, pbi_id: str) -> Path:
        valid_id = validate_pbi_id(pbi_id)
        task_root = self.base_dir / "tasks"
        task_dir = validate_safe_path(task_root, task_root / valid_id)
        task_dir.mkdir(parents=True, exist_ok=True)
        return task_dir

    def init_pbi(self, pbi_id: str, schema_path: Optional[str] = None) -> Path:
        """Initialize tasks/{pbi_id}/ directory, state.json, and component plan.md files."""
        valid_id = validate_pbi_id(pbi_id)
        task_dir = self.get_task_dir(valid_id)
        resolved_schema = schema_path or str(self.base_dir / "specs" / "api" / "v1" / f"{valid_id}.yaml")

        (task_dir / "app").mkdir(parents=True, exist_ok=True)
        (task_dir / "bff").mkdir(parents=True, exist_ok=True)

        def _init_data(data: Dict[str, Any]) -> None:
            data["pbi"] = valid_id
            data["schema_path"] = resolved_schema
            data["stage"] = "INITIALIZED"
            data["pipelines"] = {
                "app": {"phase": "INIT", "status": "PLANNED"},
                "bff": {"phase": "INIT", "status": "PLANNED"},
            }

        update_task_state(valid_id, self.base_dir, _init_data)

        # Initialize plan.md for app and bff
        for comp in ("app", "bff"):
            plan_file = task_dir / comp / "plan.md"
            if not plan_file.exists():
                content = f"""# Execution Plan: {valid_id} ({comp.upper()})

## 前提リソース
- Schema: `{resolved_schema}`
- Spec: `specs/features/{comp}/{valid_id}.feature`

## 進捗
- [x] Phase 1 & 2: 仕様・設計承認
- [ ] Phase 4-1: 設計 (DESIGN)
- [ ] Phase 4-2: TDD Red (テスト作成 & 失敗確認)
- [ ] Phase 4-3: TDD Green (実装 & テスト成功)
- [ ] Phase 4-4: Refactor (整理)

## Current Directive
- 初期化完了。設計フェーズに着手してください。
"""
                plan_file.write_text(content, encoding="utf-8")

        self.append_audit_log(valid_id, "PBI_INITIALIZED", {"schema_path": resolved_schema})
        return task_dir

    def get_state(self, pbi_id: str) -> Dict[str, Any]:
        """Read state.json safely."""
        valid_id = validate_pbi_id(pbi_id)
        return get_task_state(valid_id, self.base_dir)

    def set_component_phase(
        self, pbi_id: str, component: str, phase: str, status: str = "IN_PROGRESS"
    ) -> None:
        """Update phase of a component (app / bff) atomically."""
        valid_id = validate_pbi_id(pbi_id)
        update_pipeline_phase_atomic(valid_id, self.base_dir, component, phase, status)
        self.append_audit_log(
            valid_id,
            "PHASE_CHANGED",
            {"component": component, "phase": phase, "status": status},
        )

    def set_directive(
        self,
        pbi_id: str,
        component: str,
        directive: str,
        reason: str = "",
        instruction: str = "",
    ) -> None:
        """Update Current Directive in tasks/{pbi_id}/{component}/plan.md."""
        valid_id = validate_pbi_id(pbi_id)
        plan_file = self.get_task_dir(valid_id) / component / "plan.md"
        if not plan_file.exists():
            return

        content = plan_file.read_text(encoding="utf-8")
        lines = content.splitlines()

        new_lines = []
        in_directive = False
        for line in lines:
            if line.strip().startswith("## Current Directive"):
                in_directive = True
                new_lines.append(line)
                new_lines.append(f"- **Directive**: `{directive}`")
                if reason:
                    new_lines.append(f"- **Reason**: {reason}")
                if instruction:
                    new_lines.append(f"- **Instruction**: {instruction}")
                continue
            if in_directive and line.startswith("## "):
                in_directive = False
            if not in_directive:
                new_lines.append(line)

        plan_file.write_text("\n".join(new_lines) + "\n", encoding="utf-8")
        self.append_audit_log(
            valid_id,
            "DIRECTIVE_UPDATED",
            {"component": component, "directive": directive, "reason": reason, "instruction": instruction},
        )

    def append_audit_log(self, pbi_id: str, event: str, details: Dict[str, Any]) -> None:
        """Append audit event to tasks/{pbi_id}/audit.jsonl."""
        valid_id = validate_pbi_id(pbi_id)
        audit_file = self.get_task_dir(valid_id) / "audit.jsonl"
        log_entry = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "pbi": valid_id,
            "event": event,
            "details": details,
        }
        with open(audit_file, "a", encoding="utf-8") as f:
            f.write(json.dumps(log_entry, ensure_ascii=False) + "\n")
