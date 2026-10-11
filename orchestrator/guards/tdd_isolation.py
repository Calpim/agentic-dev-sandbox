#!/usr/bin/env python3
"""
TDD Isolation Guard for Antigravity Hook (PreToolUse)
Prevents test manipulation during Green/Refactor and prevents production edits during Red.
Also protects OpenAPI specs from direct modifications.
"""

import sys
import json
import os
import re

TEST_PATTERN = re.compile(r"(\.spec\.|\.test\.|__tests__/|Test\.[a-zA-Z]+$)")
SCHEMA_PATTERN = re.compile(r"specs[/\\]api[/\\]")

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
        tool_name = tool_call.get("name", "")
        args = tool_call.get("args", {})

        # 対象ファイルパスの取得
        target_file = args.get("TargetFile") or args.get("target_file") or args.get("path")
        if not target_file:
            respond("allow")

        norm_path = target_file.replace("\\", "/")

        # 1. スキーマ保護（常に直接変更禁止）
        if SCHEMA_PATTERN.search(norm_path):
            respond(
                "deny",
                "スキーマファイル (specs/api/**) の直接変更は禁止されています。スキーマ変更が必要な場合は差分PRまたはオーケストレーターへのエスカレーションを行ってください。"
            )

        # 2. 現在のフェーズの取得 (環境変数 TDD_PHASE または state.json から判定)
        # 環境変数がなければ安全のため allow
        current_phase = os.environ.get("TDD_PHASE", "").upper()
        if not current_phase:
            respond("allow")

        is_test_file = bool(TEST_PATTERN.search(norm_path))

        # Red フェーズ: テストのみ編集可能
        if current_phase == "RED":
            if not is_test_file:
                respond(
                    "deny",
                    f"現在 【RED】 フェーズです。プロダクションコード ({target_file}) の編集は禁止されています。失敗するテストコードのみを作成してください。"
                )

        # Green / Refactor フェーズ: テストの変更は禁止（テスト緩和の防止）
        elif current_phase in ("GREEN", "REFACTOR"):
            if is_test_file:
                respond(
                    "deny",
                    f"現在 【{current_phase}】 フェーズです。テストコード ({target_file}) の改変は禁止されています。プロダクションコードを修正してテストを Green にしてください。"
                )

        respond("allow")

    except Exception as e:
        # 異常時は開発を止めないよう警告を出して allow
        respond("allow", f"TDD Guard internal warning: {str(e)}")

if __name__ == "__main__":
    main()
