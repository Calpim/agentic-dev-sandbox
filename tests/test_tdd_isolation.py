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
        "specs/example-maps/PBI-101.md",
    ]
    for npf in non_prod_files:
        payload = make_payload("write_to_file", npf)
        res = run_guard(payload, env)
        assert res.get("decision") == "allow", f"Expected allow for {npf} in RED phase"


def test_tdd_guard_protects_guard_files_and_state_json():
    """ガード設定ファイル、ガードスクリプト、state.json はフェーズ問わず常に直接編集禁止"""
    protected_files = [
        ".agents/hooks.json",
        ".agents/tdd_isolation_hook.py",
        "orchestrator/guards/tdd_isolation.py",
        "tasks/PBI-101/state.json",
    ]
    for pf in protected_files:
        payload = make_payload("write_to_file", pf)
        for phase in ["", "RED", "GREEN", "REFACTOR"]:
            res = run_guard(payload, {"TDD_PHASE": phase})
            assert res.get("decision") == "deny", f"Expected deny for protected {pf} in {phase}"
            assert "保護ファイル" in res.get("reason", "")

    # app/tasks/worker.py は tasks を含んでもプロダクションコードなので RED 中は deny
    prod_payload = make_payload("write_to_file", "src/app/tasks/worker.py")
    res = run_guard(prod_payload, {"TDD_PHASE": "RED"})
    assert res.get("decision") == "deny"


def test_hooks_json_valid_json_without_bom():
    """hooks.json が UTF-8 (BOMなし) で正しくパースできること"""
    hooks_file = Path(__file__).resolve().parent.parent / ".agents" / "hooks.json"
    assert hooks_file.exists()
    content_bytes = hooks_file.read_bytes()
    assert not content_bytes.startswith(b"\xef\xbb\xbf"), "BOM detected in hooks.json"
    data = json.loads(content_bytes.decode("utf-8"))
    assert "tdd-isolation" in data


def test_tdd_hook_fail_closed_on_error():
    """フックスクリプトが異常終了または例外時に fail-closed (deny) となること"""
    agents_dir = Path(__file__).resolve().parent.parent / ".agents"
    bridge_script = agents_dir / "tdd_isolation_hook.py"
    if bridge_script.exists():
        # 不正な JSON を渡した場合
        proc = subprocess.Popen(
            [sys.executable, str(bridge_script)],
            cwd=str(agents_dir),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
        )
        stdout, stderr = proc.communicate("INVALID_JSON")
        assert proc.returncode == 0
        res = json.loads(stdout.strip())
        assert res.get("decision") == "deny"
        assert "fail-closed" in res.get("reason", "").lower()


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


def test_state_json_drives_tdd_isolation_for_general_paths(tmp_path: Path):
    """環境変数なしでも tasks/*/state.json のフェーズ情報から通常ソースコードへの TDD 分離が機能すること"""
    # 擬似リポジトリ構成を作成
    repo = tmp_path / "repo"
    tasks_dir = repo / "tasks" / "PBI-STATE-TEST"
    tasks_dir.mkdir(parents=True)
    state_file = tasks_dir / "state.json"

    # 1. RED フェーズ: app プロダクションコード編集は deny、テストは allow
    state_file.write_text(
        json.dumps({"pipelines": {"app": {"phase": "RED", "status": "IN_PROGRESS"}}}),
        encoding="utf-8",
    )

    # テスト対象のスクリプト実行時、repo_root を tmp_path / repo に向けるため PYTHONPATH 等を設定
    from orchestrator.guards.tdd_isolation import get_current_phase

    assert get_current_phase("src/app/models/user.py", repo) == "RED"
    assert get_current_phase("src/app/tests/test_user.py", repo) == "RED"

    # 2. GREEN フェーズ: app プロダクションコード編集は allow、テストは deny
    state_file.write_text(
        json.dumps({"pipelines": {"app": {"phase": "GREEN", "status": "IN_PROGRESS"}}}),
        encoding="utf-8",
    )
    assert get_current_phase("src/app/models/user.py", repo) == "GREEN"
    assert get_current_phase("src/app/tests/test_user.py", repo) == "GREEN"
