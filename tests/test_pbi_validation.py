from pathlib import Path

import pytest

from orchestrator.guards.validators import validate_pbi_id, validate_safe_path


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
