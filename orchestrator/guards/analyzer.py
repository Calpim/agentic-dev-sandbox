import json
import logging
import os
import re
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Dict, List

logger = logging.getLogger("orchestrator.guards.analyzer")


class DirectiveType(str, Enum):
    """ROADMAP P1-4 & WORKFLOW 5-3 に準拠した振り分け先"""

    FIX_TEST = "FIX_TEST"  # テスト自体の誤り -> Phase 4-2 (Red) へ差し戻し
    RETRY_IMPL = "RETRY_IMPL"  # 実装方針の修正 -> Phase 4-2 (Green) へ
    RETRY_DESIGN = "RETRY_DESIGN"  # 設計不整合 -> Phase 4-1 (設計) へ差し戻し
    SCHEMA_CHANGE = "SCHEMA_CHANGE"  # API契約不備 -> 軽量 HIL (人間ゲート 2 差分承認) へ
    ESCALATE_HUMAN = "ESCALATE_HUMAN"  # 要件不整合 / サイクル上限超過 -> フル HIL へ
    RESYNC = "RESYNC"  # スキーマ変更承認後の自律再同期 (P2-5 RESYNC Directive)


@dataclass
class AnalyzerDirective:
    """Analyzer が出力するディレクティブ (指示書)"""

    directive: DirectiveType
    reason: str
    instruction: str
    target_files: List[str] = field(default_factory=list)
    confidence: float = 1.0
    cycle_count: int = 1

    def to_dict(self) -> Dict[str, Any]:
        return {
            "directive": self.directive.value,
            "reason": self.reason,
            "instruction": self.instruction,
            "target_files": self.target_files,
            "confidence": self.confidence,
            "cycle_count": self.cycle_count,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "AnalyzerDirective":
        raw_directive = data.get("directive", DirectiveType.RETRY_IMPL.value)
        try:
            directive = DirectiveType(raw_directive)
        except ValueError:
            directive = DirectiveType.RETRY_IMPL

        return cls(
            directive=directive,
            reason=data.get("reason", ""),
            instruction=data.get("instruction", ""),
            target_files=data.get("target_files", []),
            confidence=float(data.get("confidence", 1.0)),
            cycle_count=int(data.get("cycle_count", 1)),
        )


def classify_failure_heuristics(
    test_output: str,
    test_code: str = "",
    impl_code: str = "",
    spec_content: str = "",
    schema_content: str = "",
    cycle_count: int = 1,
    max_cycles: int = 2,
    test_file_path: str = "",
    impl_file_path: str = "",
) -> AnalyzerDirective:
    """
    LLM が使用不可またはオフライン時の決定論的フォールバック・ルールベース分析。
    """
    # 1. サーキットブレーカー (サイクル上限超過)
    if cycle_count > max_cycles:
        return AnalyzerDirective(
            directive=DirectiveType.ESCALATE_HUMAN,
            reason=f"Analyzer サイクル回数が上限 ({max_cycles}回) を超過しました。自律ループを停止し、人間（PO）へエスカレーションします。",
            instruction="要件定義（Example Map）または設計書を確認し、手動で方針を決定してください。",
            target_files=[],
            confidence=1.0,
            cycle_count=cycle_count,
        )

    out_lower = test_output.lower()

    # 2. スキーマ契約不備の検知
    if any(k in out_lower for k in ["schema validation", "openapi", "contract mismatch", "missing schema field"]):
        target_files = [f"specs/api/v1/{Path(test_file_path).stem.replace('test_', '')}.yaml"] if test_file_path else []
        return AnalyzerDirective(
            directive=DirectiveType.SCHEMA_CHANGE,
            reason="テスト実行時に API スキーマ（OpenAPI）の不整合またはフィールド欠落が検出されました。",
            instruction="OpenAPI スキーマに必要なプロパティを追加または変更し、軽量 HIL で承認を得てください。",
            target_files=target_files,
            confidence=0.85,
            cycle_count=cycle_count,
        )

    # 3. テストコード自体の誤り (FIX_TEST) の検知
    # 例: アサーション期待値と仕様書の矛盾、テスト側の型エラーや不正なモック、フィクスチャ欠落
    is_fix_test = (
        any(
            k in out_lower
            for k in [
                "test defect",
                "wrong expectation",
                "faulty test",
                "test_assert_bug",
                "pytest: error:",
            ]
        )
        or "fixturelookuperror" in out_lower
        or ("fixture" in out_lower and "not found" in out_lower)
    )
    if is_fix_test:
        return AnalyzerDirective(
            directive=DirectiveType.FIX_TEST,
            reason="テストコード自体のアサーションまたは期待値が仕様定義と矛盾していることが検知されました。",
            instruction="テストコードの期待値・アサーション条件を Example Map 仕様書に適合するように修正してください。",
            target_files=[test_file_path] if test_file_path else [],
            confidence=0.9,
            cycle_count=cycle_count,
        )

    # 4. 設計不整合 (RETRY_DESIGN) の検知
    if any(k in out_lower for k in ["architecture violation", "design mismatch", "missing interface"]):
        return AnalyzerDirective(
            directive=DirectiveType.RETRY_DESIGN,
            reason="ドメインモデルまたはサービスインターフェースの設計不整合が原因です。",
            instruction="アーキテクチャ設計書 (design.md) を見直し、クラス責務を再構成してください。",
            target_files=[],
            confidence=0.8,
            cycle_count=cycle_count,
        )

    # 5. デフォルト: 実装方針修正 (RETRY_IMPL)
    return AnalyzerDirective(
        directive=DirectiveType.RETRY_IMPL,
        reason="テスト失敗の根本原因は実装ロジックの不備または未処理の例外です。",
        instruction="失敗箇所のトレースバックを確認し、実装コードの条件分岐やデータ処理ロジックを修正してください。",
        target_files=[impl_file_path] if impl_file_path else [],
        confidence=0.8,
        cycle_count=cycle_count,
    )


async def analyze_failure(
    pbi_id: str,
    component: str,
    test_output: str,
    test_code: str = "",
    impl_code: str = "",
    spec_content: str = "",
    schema_content: str = "",
    cycle_count: int = 1,
    max_cycles: int = 2,
    test_file_path: str = "",
    impl_file_path: str = "",
) -> AnalyzerDirective:
    """Deprecated: legacy Gemini failure analyzer; use the Analyzer subagent instead.

    This helper analyzes test output and related code/specification to select a
    recovery directive. It remains for legacy callers and tests.
    """
    logger.info(f"Analyzer triggered for {pbi_id} ({component}), cycle={cycle_count}/{max_cycles}")

    # 1. サーキットブレーカー (上限チェック)
    if cycle_count > max_cycles:
        return AnalyzerDirective(
            directive=DirectiveType.ESCALATE_HUMAN,
            reason=f"Analyzer サイクル回数が上限 ({max_cycles}回) を超過しました。無限ループを防ぐため人間へエスカレーションします。",
            instruction="人間の判断が必要です。PBI 要件またはテスト設計を手動でレビューしてください。",
            target_files=[],
            confidence=1.0,
            cycle_count=cycle_count,
        )

    api_key = os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")
    if api_key:
        try:
            from google import genai

            client = genai.Client(api_key=api_key)
            model_name = os.getenv("ANALYZER_MODEL", "gemini-2.5-pro")

            prompt = f"""あなたは自律開発パイプラインの最高推論ロール「Analyzer」です。
テスト失敗・CIエラーの根本原因を徹底分析し、後続エージェントが取るべきアクション Directive を策定してください。

【厳格な制約】
1. あなたはコードを一切書けません（読み取り専用）。
2. 出力は以下の 5 つの DirectiveType のいずれか 1 つを選択した JSON のみを出力してください：
   - FIX_TEST: テストコード側の前提や期待値が仕様と矛盾している場合（Phase 4-2 Red へ差し戻し）
   - RETRY_IMPL: 実装コードのバグやエッジケース漏れの場合（Phase 4-2 Green へ）
   - RETRY_DESIGN: 設計書やドメイン責務の根本的な不整合の場合（Phase 4-1 設計へ差し戻し）
   - SCHEMA_CHANGE: OpenAPI 契約の修正が不可避な場合（軽量 HIL スキーマ承認へ）
   - ESCALATE_HUMAN: 要件自体の矛盾、または解決不能な場合（フル HIL へ）

# PBI: {pbi_id}
# 対象コンポーネント: {component.upper()}
# 現在のサイクル: {cycle_count} / {max_cycles}

# 失敗ログ (pytest / Rule Guard):
{test_output}

# テストコード ({test_file_path}):
{test_code}

# 実装コード ({impl_file_path}):
{impl_code}

# 仕様 (Gherkin):
{spec_content}

# OpenAPI スキーマ契約:
{schema_content}

必ず以下の純粋な JSON 形式のみで出力してください（Markdown ブロック不要）：
{{
  "directive": "FIX_TEST" | "RETRY_IMPL" | "RETRY_DESIGN" | "SCHEMA_CHANGE" | "ESCALATE_HUMAN",
  "reason": "根本原因の論理的説明",
  "instruction": "後続エージェントへの具体的な行動指示",
  "target_files": ["修正すべきファイルパス"],
  "confidence": 0.95
}}
"""
            response = await client.aio.models.generate_content(
                model=model_name,
                contents=prompt,
            )
            raw_text = response.text or ""
            # JSON パース
            cleaned_json = raw_text.strip()
            if cleaned_json.startswith("```"):
                cleaned_json = re.sub(r"^```(?:json)?\n", "", cleaned_json)
                cleaned_json = re.sub(r"\n```$", "", cleaned_json)

            parsed = json.loads(cleaned_json)
            parsed["cycle_count"] = cycle_count
            directive = AnalyzerDirective.from_dict(parsed)
            logger.info(f"Analyzer LLM Decision: {directive.directive.value} (confidence={directive.confidence})")
            return directive
        except Exception as e:
            logger.warning(f"Analyzer LLM call failed or produced invalid JSON: {e}. Falling back to heuristics.")

    # 3. フォールバック (ルールベース分析)
    directive = classify_failure_heuristics(
        test_output=test_output,
        test_code=test_code,
        impl_code=impl_code,
        spec_content=spec_content,
        schema_content=schema_content,
        cycle_count=cycle_count,
        max_cycles=max_cycles,
        test_file_path=test_file_path,
        impl_file_path=impl_file_path,
    )
    logger.info(f"Analyzer Heuristic Decision: {directive.directive.value}")
    return directive
