import json
from pathlib import Path

from orchestrator.guards.analyzer import DirectiveType
from orchestrator.state_manager import StateManager


def test_directive_type_has_resync():
    """DirectiveType に RESYNC が定義されていること"""
    assert DirectiveType.RESYNC.value == "RESYNC"


def test_state_manager_resync_directive(tmp_path: Path):
    """StateManager が スキーマ更新後の RESYNC ディレクティブを plan.md および audit.jsonl に正常配信できること"""
    sm = StateManager(base_dir=tmp_path)
    pbi_id = "PBI-RESYNC-TEST"
    sm.init_pbi(pbi_id)

    # Lightweight HIL 承認後の RESYNC ディレクティブ配信
    sm.set_directive(
        pbi_id=pbi_id,
        component="app",
        directive="RESYNC",
        reason="OpenAPI schema updated via lightweight HIL",
        instruction="Resync domain models with new status field and resume TDD.",
    )

    # plan.md の検証
    plan_content = (tmp_path / "tasks" / pbi_id / "app" / "plan.md").read_text(encoding="utf-8")
    assert "- **Directive**: `RESYNC`" in plan_content
    assert "- **Reason**: OpenAPI schema updated via lightweight HIL" in plan_content
    assert "- **Instruction**: Resync domain models with new status field and resume TDD." in plan_content

    # audit.jsonl の検証
    audit_file = tmp_path / "tasks" / pbi_id / "audit.jsonl"
    logs = [json.loads(line) for line in audit_file.read_text(encoding="utf-8").splitlines()]
    resync_logs = [log for log in logs if log["event"] == "DIRECTIVE_UPDATED" and log["details"]["directive"] == "RESYNC"]
    assert len(resync_logs) == 1
    assert resync_logs[0]["details"]["component"] == "app"
    assert "OpenAPI schema updated" in resync_logs[0]["details"]["reason"]

