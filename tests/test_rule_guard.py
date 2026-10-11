import pytest
from orchestrator.guards.rule_guard import (
    classify_execution_output,
    ErrorCategory,
    NextAction,
)

def test_rule_guard_success():
    res = classify_execution_output(exit_code=0, stdout="================ 5 passed in 0.2s ================")
    assert res.category == ErrorCategory.SUCCESS
    assert res.next_action == NextAction.PROCEED

def test_rule_guard_syntax_error():
    stdout = ""
    stderr = """  File "src/service.py", line 12
    def process_data(
                    ^
SyntaxError: invalid syntax"""
    res = classify_execution_output(exit_code=1, stdout=stdout, stderr=stderr)
    assert res.category == ErrorCategory.SYNTAX_ERROR
    assert res.next_action == NextAction.DIRECT_FIX
    assert "SyntaxError" in res.summary

def test_rule_guard_type_error():
    stdout = "src/models.py:24: error: Argument 1 to \"save\" has incompatible type \"int\"; expected \"str\""
    res = classify_execution_output(exit_code=1, stdout=stdout)
    assert res.category == ErrorCategory.TYPE_ERROR
    assert res.next_action == NextAction.DIRECT_FIX
    assert "型チェックエラー" in res.summary

def test_rule_guard_lint_error():
    stdout = "src/routes.py:15:1: E302 expected 2 blank lines, found 1"
    res = classify_execution_output(exit_code=1, stdout=stdout)
    assert res.category == ErrorCategory.LINT_ERROR
    assert res.next_action == NextAction.DIRECT_FIX
    assert "Lint" in res.summary

def test_rule_guard_test_failure_within_budget():
    stdout = """============================= FAILURES =============================
___________________________ test_user_auth ___________________________
    assert response.status_code == 200
E   AssertionError: assert 401 == 200
=========================== 1 failed, 4 passed in 0.5s ==========================="""
    # retry_count = 0 (max_retries = 2) -> SELF_RETRY
    res = classify_execution_output(exit_code=1, stdout=stdout, retry_count=0, max_retries=2)
    assert res.category == ErrorCategory.TEST_FAILURE
    assert res.next_action == NextAction.SELF_RETRY
    assert "自己リトライ枠残り" in res.summary

def test_rule_guard_test_failure_exceeding_budget():
    stdout = """FAILED tests/test_user.py::test_user_auth - AssertionError: assert 401 == 200"""
    # retry_count = 2 (max_retries = 2) -> ESCALATE_ANALYZER
    res = classify_execution_output(exit_code=1, stdout=stdout, retry_count=2, max_retries=2)
    assert res.category == ErrorCategory.TEST_FAILURE
    assert res.next_action == NextAction.ESCALATE_ANALYZER
    assert "Analyzerへエスカレーション" in res.summary

def test_rule_guard_unknown_failure():
    stdout = "Segmentation fault (core dumped)"
    res = classify_execution_output(exit_code=139, stdout=stdout)
    assert res.category == ErrorCategory.UNKNOWN_FAILURE
    assert res.next_action == NextAction.ESCALATE_ANALYZER

def test_rule_guard_cli_empty_input():
    import subprocess
    import sys
    import json
    from pathlib import Path
    repo_root = Path(__file__).resolve().parent.parent
    proc = subprocess.run(
        [sys.executable, "-m", "orchestrator.guards.rule_guard"],
        input="",
        capture_output=True,
        text=True,
        encoding="utf-8",
        cwd=str(repo_root),
    )
    assert proc.returncode == 0
    data = json.loads(proc.stdout)
    assert data["category"] == ErrorCategory.UNKNOWN_FAILURE.value
    assert data["next_action"] == NextAction.ESCALATE_ANALYZER.value
    assert "summary" in data
    assert "details" in data

def test_rule_guard_cli_valid_input():
    import subprocess
    import sys
    import json
    from pathlib import Path
    repo_root = Path(__file__).resolve().parent.parent
    payload = json.dumps({"exit_code": 0, "stdout": "All tests passed"})
    proc = subprocess.run(
        [sys.executable, "-m", "orchestrator.guards.rule_guard"],
        input=payload,
        capture_output=True,
        text=True,
        encoding="utf-8",
        cwd=str(repo_root),
    )
    assert proc.returncode == 0
    data = json.loads(proc.stdout)
    assert data["category"] == ErrorCategory.SUCCESS.value
    assert data["next_action"] == NextAction.PROCEED.value
