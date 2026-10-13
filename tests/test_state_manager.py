import json
from pathlib import Path

from orchestrator.state_manager import StateManager


def test_init_pbi(tmp_path: Path):
    sm = StateManager(base_dir=tmp_path)
    pbi_id = "PBI-TEST-001"
    task_dir = sm.init_pbi(pbi_id)

    assert task_dir.exists()
    assert (task_dir / "app" / "plan.md").exists()
    assert (task_dir / "bff" / "plan.md").exists()
    assert (task_dir / "state.json").exists()
    assert (task_dir / "audit.jsonl").exists()

    state = sm.get_state(pbi_id)
    assert state["pbi"] == pbi_id
    assert state["stage"] == "INITIALIZED"
    assert "app" in state["pipelines"]
    assert "bff" in state["pipelines"]


def test_set_component_phase(tmp_path: Path):
    sm = StateManager(base_dir=tmp_path)
    pbi_id = "PBI-TEST-002"
    sm.init_pbi(pbi_id)

    sm.set_component_phase(pbi_id, "app", "RED", "IN_PROGRESS")
    state = sm.get_state(pbi_id)
    assert state["pipelines"]["app"]["phase"] == "RED"
    assert state["pipelines"]["app"]["status"] == "IN_PROGRESS"

    # Verify audit log appended
    audit_file = tmp_path / "tasks" / pbi_id / "audit.jsonl"
    logs = [json.loads(line) for line in audit_file.read_text(encoding="utf-8").splitlines()]
    assert any(log["event"] == "PHASE_CHANGED" and log["details"]["phase"] == "RED" for log in logs)


def test_set_directive(tmp_path: Path):
    sm = StateManager(base_dir=tmp_path)
    pbi_id = "PBI-TEST-003"
    sm.init_pbi(pbi_id)

    sm.set_directive(
        pbi_id,
        component="app",
        directive="FIX_TEST",
        reason="Mock assertion mismatch",
        instruction="Update expected status code to 400",
    )

    plan_content = (tmp_path / "tasks" / pbi_id / "app" / "plan.md").read_text(encoding="utf-8")
    assert "- **Directive**: `FIX_TEST`" in plan_content
    assert "- **Reason**: Mock assertion mismatch" in plan_content
    assert "- **Instruction**: Update expected status code to 400" in plan_content

    state = sm.get_state(pbi_id)
    assert state["pipelines"]["app"]["directive"] == "FIX_TEST"



def test_set_directive_fallback(tmp_path: Path):
    sm = StateManager(base_dir=tmp_path)
    pbi_id = "PBI-TEST-004"
    task_dir = sm.get_task_dir(pbi_id)
    app_dir = task_dir / "app"
    app_dir.mkdir(parents=True, exist_ok=True)
    plan_file = app_dir / "plan.md"
    plan_file.write_text("# Plan without directive section\n\nSome text.\n", encoding="utf-8")

    sm.set_directive(
        pbi_id,
        component="app",
        directive="RETRY_IMPL",
        reason="Syntax error",
    )

    updated_content = plan_file.read_text(encoding="utf-8")
    assert "## Current Directive" in updated_content
    assert "- **Directive**: `RETRY_IMPL`" in updated_content
    assert "- **Reason**: Syntax error" in updated_content
