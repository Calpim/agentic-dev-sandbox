from pathlib import Path

import pytest

from orchestrator.guards.validators import validate_pbi_id, validate_safe_path
from orchestrator.workflow import PBIDevelopmentWorkflow


def test_validate_pbi_id_valid():
    valid_ids = ["PBI-1", "PBI-101", "PBI-USER-001", "PBI-FEATURE_ALPHA-99"]
    for pbi in valid_ids:
        assert validate_pbi_id(pbi) == pbi
        # Leading/trailing whitespace should be stripped
        assert validate_pbi_id(f"  {pbi}  ") == pbi


def test_validate_pbi_id_invalid():
    invalid_ids = [
        "",
        "   ",
        None,
        123,
        "101",
        "ISSUE-101",
        "pbi-101",  # must match PBI-
        "PBI-101/../../etc",
        "../PBI-101",
        "PBI-101; rm -rf /",
        "PBI-101&exit",
        "PBI-101\0",
        "PBI 101",
    ]
    for bad_id in invalid_ids:
        with pytest.raises(ValueError):
            validate_pbi_id(bad_id)  # type: ignore


def test_validate_safe_path(tmp_path: Path):
    base_dir = tmp_path / "workspace"
    base_dir.mkdir()
    child_file = base_dir / "specs" / "test.md"
    child_file.parent.mkdir()
    child_file.write_text("ok")

    assert validate_safe_path(base_dir, child_file) == child_file.resolve()

    traversal_path = base_dir / ".." / "outside.txt"
    with pytest.raises(ValueError, match="Path traversal detected"):
        validate_safe_path(base_dir, traversal_path)


def test_workflow_signal_stage_guard():
    wf = PBIDevelopmentWorkflow()

    # 1. INIT stage: all signals should be ignored
    wf.state.stage = "INIT"
    wf.approve_example_map()
    assert wf.state.example_map_approved is False
    assert len(wf.state.ignored_signals) == 1
    assert "approve_example_map" in wf.state.ignored_signals[0]

    wf.approve_schema()
    assert wf.state.schema_approved is False
    assert len(wf.state.ignored_signals) == 2
    assert "approve_schema" in wf.state.ignored_signals[1]

    wf.approve_schema_change("approved diff")
    assert wf.state.schema_change_approved is False
    assert len(wf.state.ignored_signals) == 3
    assert "approve_schema_change" in wf.state.ignored_signals[2]

    # 2. Gate 1 stage: approve_example_map / reject_example_map accepted, others ignored
    wf.state.stage = "WAITING_HUMAN_GATE_1_EXAMPLE_MAP"
    wf.approve_example_map()
    assert wf.state.example_map_approved is True
    assert len(wf.state.ignored_signals) == 3  # unchanged

    wf.approve_schema()
    assert wf.state.schema_approved is False
    assert len(wf.state.ignored_signals) == 4

    # 3. Gate 2 stage: approve_schema accepted, Gate 1 ignored
    wf.state.stage = "WAITING_HUMAN_GATE_2_SCHEMA"
    wf.approve_schema()
    assert wf.state.schema_approved is True
    assert len(wf.state.ignored_signals) == 4  # unchanged

    wf.reject_example_map("try again")
    assert wf.state.example_map_feedback is None
    assert len(wf.state.ignored_signals) == 5

    # 4. Lightweight HIL stage: approve_schema_change accepted
    wf.state.stage = "WAITING_LIGHTWEIGHT_HIL_SCHEMA_CHANGE"
    wf.approve_schema_change("looks good")
    assert wf.state.schema_change_approved is True
    assert wf.state.schema_change_feedback == "looks good"
    assert len(wf.state.ignored_signals) == 5  # unchanged
