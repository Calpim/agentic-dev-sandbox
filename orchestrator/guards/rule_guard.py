import json
import re
import sys
from dataclasses import dataclass
from enum import Enum
from typing import Any, Dict


class ErrorCategory(str, Enum):
    SUCCESS = "SUCCESS"
    SYNTAX_ERROR = "SYNTAX_ERROR"
    COLLECTION_ERROR = "COLLECTION_ERROR"
    TIMEOUT_ERROR = "TIMEOUT_ERROR"
    TYPE_ERROR = "TYPE_ERROR"
    LINT_ERROR = "LINT_ERROR"
    TEST_FAILURE = "TEST_FAILURE"
    UNKNOWN_FAILURE = "UNKNOWN_FAILURE"


class NextAction(str, Enum):
    PROCEED = "PROCEED"  # 成功: 次のフェーズへ進行
    DIRECT_FIX = "DIRECT_FIX"  # 機械的エラー: Implementer にエラー全文を返して即時修正 (0トークン)
    SELF_RETRY = "SELF_RETRY"  # テスト失敗 (予算内): Implementer の自己リトライへ
    ESCALATE_ANALYZER = "ESCALATE_ANALYZER"  # リトライ上限超過 or 原因不明: Analyzer (Opus) へエスカレーション


@dataclass
class RuleGuardResult:
    category: ErrorCategory
    next_action: NextAction
    summary: str
    details: str
    retry_count: int = 0
    max_retries: int = 2

    def to_dict(self) -> Dict[str, Any]:
        return {
            "category": self.category.value,
            "next_action": self.next_action.value,
            "summary": self.summary,
            "details": self.details,
            "retry_count": self.retry_count,
            "max_retries": self.max_retries,
        }


def classify_execution_output(
    exit_code: int, stdout: str, stderr: str = "", retry_count: int = 0, max_retries: int = 2
) -> RuleGuardResult:
    """
    CI / テスト / Lint の実行出力を LLM を介さずに 0 トークンで決定論的に分類する。
    ROADMAP P1-3 & WORKFLOW 5-2 に準拠。
    """
    combined_output = f"{stdout}\n{stderr}".strip()

    # 1. 正常終了
    if exit_code == 0:
        return RuleGuardResult(
            category=ErrorCategory.SUCCESS,
            next_action=NextAction.PROCEED,
            summary="All verification checks passed successfully.",
            details="",
            retry_count=retry_count,
            max_retries=max_retries,
        )

    # 2. タイムアウト (終了コード -1 または TimeoutError 出力)
    if exit_code == -1 or "TimeoutError" in combined_output or "pytest execution timed out" in combined_output:
        return RuleGuardResult(
            category=ErrorCategory.TIMEOUT_ERROR,
            next_action=NextAction.ESCALATE_ANALYZER,
            summary="テスト実行タイムアウト検知 (規定時間を超過しました)",
            details=combined_output,
            retry_count=retry_count,
            max_retries=max_retries,
        )

    # 3. 構文エラー (Syntax / Indentation / Parse error)
    syntax_patterns = [
        r"SyntaxError:\s*(.+)",
        r"IndentationError:\s*(.+)",
        r"invalid syntax",
        r"ParseError:\s*(.+)",
        r"Syntax Error",
    ]
    for pattern in syntax_patterns:
        match = re.search(pattern, combined_output, re.IGNORECASE)
        if match:
            detail = match.group(0)
            return RuleGuardResult(
                category=ErrorCategory.SYNTAX_ERROR,
                next_action=NextAction.DIRECT_FIX,
                summary=f"構文エラー検知: {detail}",
                details=combined_output,
                retry_count=retry_count,
                max_retries=max_retries,
            )

    # 4. テスト収集エラー (pytest collection error, exit code 2, ERROR collecting)
    if (
        exit_code == 2
        or re.search(r"ERROR collecting\s+(.+)", combined_output, re.IGNORECASE)
        or re.search(r"UsageError:\s*(.+)", combined_output, re.IGNORECASE)
    ):
        match = re.search(r"ERROR collecting\s+(.+)", combined_output, re.IGNORECASE) or re.search(
            r"UsageError:\s*(.+)", combined_output, re.IGNORECASE
        )
        detail = match.group(0) if match else "pytest collection error (exit code 2)"
        return RuleGuardResult(
            category=ErrorCategory.COLLECTION_ERROR,
            next_action=NextAction.DIRECT_FIX,
            summary=f"テスト収集・実行前エラー検知: {detail}",
            details=combined_output,
            retry_count=retry_count,
            max_retries=max_retries,
        )

    # 3. 型エラー (mypy, tsc, TypeError)
    type_patterns = [
        r"error:\s*Argument \d+ to .+ has incompatible type",
        r"error:\s*Incompatible types in assignment",
        r"error:\s*Item .+ of .+ has no attribute",
        r"error:\s*Missing return statement",
        r"Type '.+' is not assignable to type '.+'",
        r"Found \d+ errors? in \d+ files? \(checked \d+ source files?\)",
        r"TypeError:\s*(.+)",
    ]
    for pattern in type_patterns:
        match = re.search(pattern, combined_output, re.IGNORECASE)
        if match:
            detail = match.group(0)
            return RuleGuardResult(
                category=ErrorCategory.TYPE_ERROR,
                next_action=NextAction.DIRECT_FIX,
                summary=f"型チェックエラー検知: {detail}",
                details=combined_output,
                retry_count=retry_count,
                max_retries=max_retries,
            )

    # 4. Lint / スタイルエラー (flake8, eslint, ruff)
    lint_patterns = [
        r"(?:[^\s:]+\.py):\d+:\d+:\s*(?:[A-Z]\d{3,4}|[A-Z]+[0-9]+)\s*(.+)",
        r"ESLint:\s*\d+ problems?",
        r"rule violation:\s*(.+)",
        r"line \d+, col \d+, Error - (.+)",
    ]
    for pattern in lint_patterns:
        match = re.search(pattern, combined_output, re.IGNORECASE)
        if match:
            detail = match.group(0)
            return RuleGuardResult(
                category=ErrorCategory.LINT_ERROR,
                next_action=NextAction.DIRECT_FIX,
                summary=f"Lint / コード規約違反検知: {detail}",
                details=combined_output,
                retry_count=retry_count,
                max_retries=max_retries,
            )

    # 5. テスト失敗 (pytest, jest, unittest, assertion error, pytest.fail)
    test_failure_patterns = [
        r"FAILED\s+([^\s]+::[^\s]+)",
        r"AssertionError:\s*(.+)",
        r"AssertionError",
        r"Failed:\s*(.+)",
        r"NotImplementedError",
        r"\d+ failed,\s*\d+ passed",
        r"FAIL:\s*(.+)",
        r"Tests failed:",
    ]
    for pattern in test_failure_patterns:
        match = re.search(pattern, combined_output, re.IGNORECASE)
        if match:
            detail = match.group(0)
            if retry_count < max_retries:
                return RuleGuardResult(
                    category=ErrorCategory.TEST_FAILURE,
                    next_action=NextAction.SELF_RETRY,
                    summary=f"テストアサーション失敗 (自己リトライ枠残り: {max_retries - retry_count}): {detail}",
                    details=combined_output,
                    retry_count=retry_count,
                    max_retries=max_retries,
                )
            else:
                return RuleGuardResult(
                    category=ErrorCategory.TEST_FAILURE,
                    next_action=NextAction.ESCALATE_ANALYZER,
                    summary=f"テストアサーション失敗 (自己リトライ上限 {max_retries} 回超過 -> Analyzerへエスカレーション): {detail}",
                    details=combined_output,
                    retry_count=retry_count,
                    max_retries=max_retries,
                )

    # 6. 未知 / 想定外の失敗
    return RuleGuardResult(
        category=ErrorCategory.UNKNOWN_FAILURE,
        next_action=NextAction.ESCALATE_ANALYZER,
        summary=f"未知の実行時エラー (終了コード {exit_code}) -> Analyzerへエスカレーション",
        details=combined_output,
        retry_count=retry_count,
        max_retries=max_retries,
    )


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    if hasattr(sys.stdin, "reconfigure"):
        sys.stdin.reconfigure(encoding="utf-8")

    raw_input = sys.stdin.read()
    if not raw_input.strip():
        empty_res = RuleGuardResult(
            category=ErrorCategory.UNKNOWN_FAILURE,
            next_action=NextAction.ESCALATE_ANALYZER,
            summary="Empty input received by Rule Guard",
            details="",
        )
        print(json.dumps(empty_res.to_dict(), ensure_ascii=False, indent=2))
        sys.exit(0)

    try:
        data = json.loads(raw_input)
        exit_code = data.get("exit_code", 1)
        stdout = data.get("stdout", "")
        stderr = data.get("stderr", "")
        retry_count = data.get("retry_count", 0)
        max_retries = data.get("max_retries", 2)
        result = classify_execution_output(exit_code, stdout, stderr, retry_count, max_retries)
        print(json.dumps(result.to_dict(), ensure_ascii=False, indent=2))
    except Exception as e:
        err_res = RuleGuardResult(
            category=ErrorCategory.UNKNOWN_FAILURE,
            next_action=NextAction.ESCALATE_ANALYZER,
            summary=f"Rule Guard execution error: {e}",
            details=str(e),
        )
        print(json.dumps(err_res.to_dict(), ensure_ascii=False, indent=2))
