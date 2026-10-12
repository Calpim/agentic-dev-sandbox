import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Optional

GUARD_SCRIPT = Path(__file__).resolve().parent.parent / "orchestrator" / "guards" / "tdd_isolation.py"


def run_guard(payload: dict, env_vars: Optional[dict] = None) -> dict:
    """Helper to run tdd_isolation.py with mock stdin and env vars"""
    env = os.environ.copy()
    if env_vars:
        env.update(env_vars)

    proc = subprocess.Popen(
        [sys.executable, str(GUARD_SCRIPT)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        env=env,
    )
    stdout, stderr = proc.communicate(json.dumps(payload))
    assert proc.returncode == 0, f"Guard script crashed: {stderr}"
    return json.loads(stdout.strip())


def make_payload(tool_name: str, target_file: str) -> dict:
    return {"toolCall": {"name": tool_name, "args": {"TargetFile": target_file}}}


def test_default_phase_allows_any_file():
    payload = make_payload("write_to_file", "src/models/user.py")
    res = run_guard(payload, {"TDD_PHASE": ""})
    assert res.get("decision") == "allow"


def test_schema_protection_always_denies():
    # スキーマファイルはフェーズ問わず常に直接編集不可
    for phase in ["", "RED", "GREEN", "REFACTOR"]:
        payload = make_payload("write_to_file", "specs/api/v1/auth.yaml")
        res = run_guard(payload, {"TDD_PHASE": phase})
        assert res.get("decision") == "deny"
        assert "スキーマファイル" in res.get("reason", "")


def test_red_phase_permissions():
    env = {"TDD_PHASE": "RED"}

    # 失敗するテストの作成・編集は許可
    test_files = [
        "tests/unit/user.spec.ts",
        "tests/test_authentication.py",
        "src/__tests__/component.test.jsx",
        "test/DomainModelTest.java",
    ]
    for tf in test_files:
        payload = make_payload("write_to_file", tf)
        res = run_guard(payload, env)
        assert res.get("decision") == "allow", f"Failed for {tf}"

    # プロダクションコードの作成・編集は拒否
    prod_files = [
        "src/domain/user.ts",
        "orchestrator/activities.py",
        "backend/models.py",
    ]
    for pf in prod_files:
        payload = make_payload("write_to_file", pf)
        res = run_guard(payload, env)
        assert res.get("decision") == "deny", f"Should deny {pf} in RED"
        assert "RED" in res.get("reason", "")


def test_green_phase_permissions():
    env = {"TDD_PHASE": "GREEN"}

    # プロダクションコードの実装は許可
    payload = make_payload("write_to_file", "src/domain/user.ts")
    res = run_guard(payload, env)
    assert res.get("decision") == "allow"

    # テストの改ざん・緩和は拒否（自作自演防止）
    test_payload = make_payload("replace_file_content", "tests/unit/user.spec.ts")
    res = run_guard(test_payload, env)
    assert res.get("decision") == "deny"
    assert "GREEN" in res.get("reason", "")


def test_refactor_phase_permissions():
    env = {"TDD_PHASE": "REFACTOR"}

    # プロダクションコードの整理は許可
    payload = make_payload("replace_file_content", "src/domain/user.ts")
    res = run_guard(payload, env)
    assert res.get("decision") == "allow"

    # テストの編集は拒否
    test_payload = make_payload("replace_file_content", "tests/unit/user.spec.ts")
    res = run_guard(test_payload, env)
    assert res.get("decision") == "deny"
    assert "REFACTOR" in res.get("reason", "")


def test_empty_or_invalid_payload_safe_fallback():
    # 不正な入力でも例外でエージェントを止めない
    proc = subprocess.Popen(
        [sys.executable, str(GUARD_SCRIPT)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        text=True,
        encoding="utf-8",
    )
    stdout, _ = proc.communicate("")
    res = json.loads(stdout.strip())
    assert res.get("decision") == "allow"


def test_non_production_files_allowed_in_red_phase():
    """REDフェーズでもscratchファイル、タスク計画書、ドキュメント等の非プロダクションファイルは許可される"""
    env = {"TDD_PHASE": "RED"}
    non_prod_files = [
        r"C:\Users\cal7m\.gemini\antigravity\brain\test\scratch\script.py",
        "tasks/PBI-101/app/plan.md",
        "docs/PROGRESS.md",
        ".agents/hooks.json",
        "specs/example-maps/PBI-101.md",
    ]
    for npf in non_prod_files:
        payload = make_payload("write_to_file", npf)
        res = run_guard(payload, env)
        assert res.get("decision") == "allow", f"Expected allow for {npf} in RED phase"


def test_hook_execution_from_agents_directory():
    """カレントディレクトリが .agents 配下であっても ModuleNotFoundError にならず正常終了すること (Issue #20)"""
    agents_dir = Path(__file__).resolve().parent.parent / ".agents"
    bridge_script = agents_dir / "tdd_isolation_hook.py"
    if bridge_script.exists():
        proc = subprocess.Popen(
            [sys.executable, str(bridge_script)],
            cwd=str(agents_dir),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
        )
        stdout, stderr = proc.communicate(json.dumps(make_payload("write_to_file", "tasks/plan.md")))
        assert proc.returncode == 0, f"Hook failed with stderr: {stderr}"
        res = json.loads(stdout.strip())
        assert res.get("decision") == "allow"

