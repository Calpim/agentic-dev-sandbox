#!/usr/bin/env python3
"""
TDD Isolation Guard for Antigravity Hook (PreToolUse)
Prevents test manipulation during Green/Refactor and prevents production edits during Red.
Also protects OpenAPI specs, state.json, and guard configurations from direct modifications.
Fail-closed policy: Denies execution on guard error.
"""

import json
import os
import re
import sys
try:
    sys.stdin.reconfigure(encoding='utf-8')
    sys.stdout.reconfigure(encoding='utf-8')
except Exception:
    pass
from pathlib import Path

TEST_PATTERN = re.compile(
    r"(\.spec\.[a-zA-Z0-9]+$|\.test\.[a-zA-Z0-9]+$|__tests__/|[/\\]tests?[/\\]|([/\\]|^)test_[^/\\]+\.py$|([/\\]|^)[^/\\]+_test\.py$|Test\.[a-zA-Z]+$)",
    re.IGNORECASE,
)
SCHEMA_PATTERN = re.compile(r"^specs/api/", re.IGNORECASE)

# ガード設定ファイル・ガード本体・状態正本の保護 (フェーズ不問で直接編集禁止)
PROTECTED_CONFIG_PATTERN = re.compile(
    r"^(\.agents/hooks\.json$|\.agents/tdd_isolation_hook\.py$|orchestrator/guards/|tasks/[^/]+/state\.json$)",
    re.IGNORECASE,
)

# 開発エージェントが作業用・仕様検討用として編集を許可されるファイル (アンカー付き)
ALLOWED_NON_PROD_PATTERN = re.compile(
    r"^(scratch/|docs/|specs/example-maps/|specs/features/|tasks/.*\.md$|tasks/.*\.jsonl$)",
    re.IGNORECASE,
)


def normalize_rel_path(target_path: str, repo_root: Path) -> str:
    """ターゲットパスをリポジトリルート基準の相対パスに正規化する"""
    p = Path(target_path)
    try:
        if p.is_absolute():
            return p.resolve().relative_to(repo_root.resolve()).as_posix()
    except Exception:
        pass
    norm = target_path.replace("\\", "/").strip()
    while norm.startswith("./"):
        norm = norm[2:]
    return norm.lstrip("/")


def get_current_phase(rel_path: str, repo_root: Path) -> str:
    """
    tasks/**/state.json からコンポーネントに応じた TDD フェーズを特定する (Single Source of Truth).
    テスト時や環境変数指定時は明示的フェーズを優先。
    """
    # 1. 単体テスト・環境変数明示指定 (TDD_GUARD_TEST_OVERRIDE_PHASE または TDD_PHASE)
    for env_key in ("TDD_GUARD_TEST_OVERRIDE_PHASE", "TDD_PHASE"):
        if env_key in os.environ:
            val = os.environ[env_key].strip().upper()
            if val in ("RED", "GREEN", "REFACTOR"):
                return val
            if val == "":
                return ""

    tasks_dir = repo_root / "tasks"

    # 2. state.json からのコンポーネント別フェーズ特定 (Single Source of Truth)
    if tasks_dir.exists():
        # 対象パスに PBI ID (PBI-xxx) が含まれているか確認
        pbi_match = re.search(r"(PBI-[A-Za-z0-9_-]+)", rel_path, re.IGNORECASE)
        target_pbi = pbi_match.group(1).upper() if pbi_match else None

        pbi_dirs = [d for d in tasks_dir.iterdir() if d.is_dir()]
        # target_pbi と一致するディレクトリのみを対象とする
        if target_pbi:
            pbi_dirs = [d for d in pbi_dirs if d.name.upper() == target_pbi]
        else:
            pbi_dirs = []

        for pbi_dir in pbi_dirs:
            state_file = pbi_dir / "state.json"
            if state_file.is_file():
                try:
                    state_data = json.loads(state_file.read_text(encoding="utf-8"))
                    pipelines = state_data.get("pipelines", {})
                    # パスに app または bff が含まれているか確認
                    comp = (
                        "app"
                        if ("app/" in rel_path or "/app" in rel_path)
                        else "bff"
                        if ("bff/" in rel_path or "/bff" in rel_path)
                        else None
                    )
                    if comp and comp in pipelines and isinstance(pipelines[comp], dict):
                        p_phase = pipelines[comp].get("phase", "").upper()
                        if p_phase in ("RED", "GREEN", "REFACTOR"):
                            return p_phase
                    # コンポーネント特定できない場合はアクティブなフェーズを探索
                    for c in ("app", "bff"):
                        if c in pipelines and isinstance(pipelines[c], dict):
                            c_phase = pipelines[c].get("phase", "").upper()
                            if c_phase in ("RED", "GREEN", "REFACTOR"):
                                return c_phase
                except Exception:
                    pass

    return ""


def respond(decision: str, reason: str = ""):
    output = {"decision": decision}
    if reason:
        output["reason"] = reason
    print(json.dumps(output))
    sys.exit(0)


def main():
    try:
        raw_input = sys.stdin.read()
        if not raw_input.strip():
            respond("allow")

        payload = json.loads(raw_input)
        tool_call = payload.get("toolCall", {})
        args = tool_call.get("args", {})

        # 対象ファイルパスの取得
        target_file = args.get("TargetFile") or args.get("target_file") or args.get("path")
        if not target_file:
            respond("allow")

        repo_root = Path(__file__).resolve().parent.parent.parent
        rel_path = normalize_rel_path(target_file, repo_root)

        # 1. ガード設定および状態管理ファイルの自己防護（直接変更禁止）
        if PROTECTED_CONFIG_PATTERN.search(rel_path):
            respond(
                "deny",
                f"保護ファイル ({target_file}) の直接編集は禁止されています。ガード設定・フック・state.json はシステム保護対象です。",
            )

        # 2. スキーマ保護（常に直接変更禁止）
        if SCHEMA_PATTERN.search(rel_path):
            respond(
                "deny",
                "スキーマファイル (specs/api/**) の直接変更は禁止されています。スキーマ変更が必要な場合は差分PRまたはオーケストレーターへのエスカレーションを行ってください。",
            )

        # 3. 許可された作業用ファイル（scratch/, docs/, plan.md 等）はフェーズ問わず allow
        if ALLOWED_NON_PROD_PATTERN.search(rel_path):
            respond("allow")

        # ワークスペース外作業ファイル（.gemini など）も allow
        if ".gemini/" in rel_path or "brain/" in rel_path:
            respond("allow")

        # 4. 現在のフェーズの取得
        current_phase = get_current_phase(rel_path, repo_root)
        if not current_phase:
            respond("allow")

        is_test_file = bool(TEST_PATTERN.search(rel_path))

        # Red フェーズ: テストのみ編集可能 (プロダクションコードは禁止)
        if current_phase == "RED":
            if not is_test_file:
                respond(
                    "deny",
                    f"現在 【RED】 フェーズです。プロダクションコード ({target_file}) の編集は禁止されています。失敗するテストコードのみを作成してください。",
                )

        # Green / Refactor フェーズ: テストの変更は禁止（テスト緩和の防止）
        elif current_phase in ("GREEN", "REFACTOR"):
            if is_test_file:
                respond(
                    "deny",
                    f"現在 【{current_phase}】 フェーズです。テストコード ({target_file}) の改変は禁止されています。プロダクションコードを修正してテストを Green にしてください。",
                )

        respond("allow")

    except Exception as e:
        # fail-closed: 異常時は安全のため拒否
        respond("deny", f"TDD Guard error (fail-closed): {str(e)}")


if __name__ == "__main__":
    main()
