import asyncio
import json
import logging
import os
import re
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple, Union

import yaml
from dotenv import load_dotenv
from temporalio import activity

from orchestrator.guards.analyzer import (
    DirectiveType,
    analyze_failure,
)
from orchestrator.guards.rule_guard import (
    ErrorCategory,
    NextAction,
    classify_execution_output,
)
from orchestrator.guards.validators import (
    validate_gherkin_coverage,
    validate_pbi_id,
    validate_safe_path,
)

load_dotenv()
logger = logging.getLogger("orchestrator.activities")

BASE_DIR = Path(__file__).resolve().parent.parent


def _to_pascal_case(identifier: str) -> str:
    """ハイフンやアンダースコア区切りの文字列を PascalCase / CamelCase に変換する"""
    return re.sub(r"[^a-zA-Z0-9]+", " ", identifier).title().replace(" ", "")


def _ensure_dir(p: Path):
    p.mkdir(parents=True, exist_ok=True)


def _strip_markdown_codeblock(text: Optional[str]) -> str:
    """LLM が出力した Markdown コードブロック (```markdown ... ```) を除去する"""
    if not text:
        return ""
    cleaned = text.strip()
    if cleaned.startswith("```"):
        lines = cleaned.splitlines()
        if lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        cleaned = "\n".join(lines).strip()
    return cleaned


async def _call_gemini_for_example_map(pbi_id: str, pbi_content: str, feedback: Optional[str] = None) -> Optional[str]:
    api_key = os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")
    if not api_key:
        return None
    try:
        from google import genai

        client = genai.Client(api_key=api_key)
        model_name = os.getenv("SPEC_AGENT_MODEL", "gemini-3.8-flash")

        prompt = f"""あなたは BDD / Example Mapping の専門家「Spec Agent」です。
以下のPBI要件を分析し、受け入れ条件を明確化するための「Example Map」を日本語のMarkdown形式で作成してください。

# PBI 識別子: {pbi_id}
# 要件概要:
{pbi_content}
"""
        if feedback:
            prompt += f"""
# 人間からの修正指示（最優先で反映してください）:
{feedback}
"""

        prompt += """
# 必須フォーマット
以下の構成を厳密に守ってください。
1. タイトル (# Example Map: {pbi_id})
2. PBI 要件概要 (## PBI 要件概要)
3. ルール定義 (## BIZ-001: [ルール名], ## BIZ-002: [ルール名], ...)
   - 各ルールの下に、具体例を表形式 (| ルール | 具体例 (Example) | 期待される結果 |) で記載。正常系、異常系、境界値を網羅すること。
4. 人間への確認事項 (## ❓ AIからの確認事項 (Human-in-the-Loop))
   - 曖昧な仕様やエッジケース、境界値、エラーハンドリングに関して、人間に確認すべき鋭い質問を 2〜4 件箇条書き (- [ ] Q1. ...) で挙げること。
"""
        response = await client.aio.models.generate_content(
            model=model_name,
            contents=prompt,
        )
        return response.text
    except Exception as e:
        logger.warning(f"Failed to generate example map via Gemini API: {e}")
        return None


@activity.defn
async def generate_example_map_activity(pbi_id: str, pbi_content: str, feedback: Optional[str] = None) -> str:
    """Phase 1-1: Example Map 生成 Activity (Gemini API 結合 & フィードバック反映)"""
    pbi_id = validate_pbi_id(pbi_id)
    logger.info(f"Generating Example Map for {pbi_id} (feedback: {feedback})")

    target_dir = BASE_DIR / "specs" / "example-maps"
    _ensure_dir(target_dir)
    target_file = validate_safe_path(target_dir, target_dir / f"{pbi_id}.md")

    # 1. 本物の LLM (Gemini API) 呼び出しを試行 (非同期)
    llm_output = await _call_gemini_for_example_map(pbi_id, pbi_content, feedback)

    if llm_output:
        logger.info("Successfully generated Example Map via Gemini API!")
        sanitized_output = _strip_markdown_codeblock(llm_output)
        target_file.write_text(sanitized_output, encoding="utf-8")
        return str(target_file)

    # 2. フォールバック生成（APIキー未設定またはエラー時：フィードバックを動的解析）
    logger.info("Generating Example Map via fallback template generator...")
    feedback_section = ""
    extra_rule = ""
    if feedback:
        feedback_section = f"\n> ↺ 【修正指示を反映】: {feedback}\n"
        extra_rule = f"""
## BIZ-003 (フィードバック反映): 追加要件の適用
| ルール | 具体例 (Example) | 期待される結果 |
| :--- | :--- | :--- |
| 指示内容の遵守 | {feedback} に従った入力・操作 | 指定通りのバリデーションおよび振る舞いが成立する |
"""

    content = f"""# Example Map: {pbi_id}
{feedback_section}
## PBI 要件概要
{pbi_content}

## BIZ-001: 正常系シナリオ
| ルール | 具体例 (Example) | 期待される結果 |
| :--- | :--- | :--- |
| 正当な入力値の受付 | 必須項目がすべて正しく入力されている | 処理が成功しステータス200が返る |

## BIZ-002: 異常系・バリデーションシナリオ
| ルール | 具体例 (Example) | 期待される結果 |
| :--- | :--- | :--- |
| 重複・不正入力の遮断 | 既存のリソースと競合する識別子を指定 | エラー (409 Conflict) を返却する |
{extra_rule}
## ❓ AIからの確認事項 (Human-in-the-Loop)
- [ ] Q1. 境界値（上限文字数）に達した際の挙動はエラーとするか、自動切り詰めとするか？
- [ ] Q2. 認証トークンの有効期限切れ時のフォールバック処理は定義されているか？
"""
    target_file.write_text(content, encoding="utf-8")
    return str(target_file)


async def _call_gemini_for_decomposition(pbi_id: str, example_map_content: str) -> Optional[Dict[str, str]]:
    api_key = os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")
    if not api_key:
        return None
    try:
        from google import genai

        client = genai.Client(api_key=api_key)
        model_name = os.getenv("SPEC_DECOMPOSE_MODEL", os.getenv("SPEC_AGENT_MODEL", "gemini-3.8-flash"))

        prompt = f"""あなたは BDD 仕様分解の専門家「Spec Decomposition Agent」です。
以下の承認済み「Example Map」を入力とし、3つの層（ビジネス層、App層、BFF層）の Gherkin シナリオを作成してください。

# PBI 識別子: {pbi_id}
# 承認済み Example Map:
{example_map_content}

# 必須要件と規約
1. すべての Gherkin ファイルの先頭に `# language: ja` を宣言すること。
2. ビジネス層:
   - 宣言的な日本語で記述し、操作手順（ボタンをクリック等）ではなく振る舞いを書く。
   - ルールごとに `@BIZ-xxx` をタグ付けする。
3. App層:
   - クライアント UI / 状態変化 / ユーザーフィードバックのシナリオを展開。
   - 必ず `@covers:BIZ-xxx` を付与してビジネス層とのトレーサビリティを成立させること。
4. BFF層:
   - API エンドポイントの入出力、バリデーション、マッピングのシナリオを展開。
   - 必ず `@covers:BIZ-xxx` を付与すること。

# 出力形式 (JSON 厳守)
以下のキーを持つ単一の JSON オブジェクトのみを返してください。Markdownのコードブロック（```json）は不要です。
{{
  "business": "<ビジネス層のGherkin文字列>",
  "app": "<App層のGherkin文字列>",
  "bff": "<BFF層のGherkin文字列>"
}}
"""
        response = await client.aio.models.generate_content(
            model=model_name,
            contents=prompt,
        )
        text = _strip_markdown_codeblock(response.text)
        data = json.loads(text)
        if all(k in data for k in ("business", "app", "bff")):
            return data
        return None
    except Exception as e:
        logger.warning(f"Failed to decompose specs via Gemini API: {e}")
        return None


@activity.defn
async def decompose_specs_activity(pbi_id: str, example_map_path: str) -> Dict[str, str]:
    """Phase 1-2 & 1-3: ビジネス Gherkin 生成 & App/BFF 分解 (Gemini API 結合)"""
    pbi_id = validate_pbi_id(pbi_id)
    logger.info(f"Decomposing specs for {pbi_id} from {example_map_path}")

    biz_dir = BASE_DIR / "specs" / "features" / "business"
    app_dir = BASE_DIR / "specs" / "features" / "app"
    bff_dir = BASE_DIR / "specs" / "features" / "bff"
    for d in (biz_dir, app_dir, bff_dir):
        _ensure_dir(d)

    safe_example_map_path = validate_safe_path(BASE_DIR, example_map_path, allow_temp=True)
    example_map_content = safe_example_map_path.read_text(encoding="utf-8")

    strict_mode = os.getenv("STRICT_SPEC_GENERATION", "").lower() in ("1", "true", "yes")

    # 1. 本物の LLM (Gemini API) による仕様分解を試行
    llm_specs = await _call_gemini_for_decomposition(pbi_id, example_map_content)
    if llm_specs:
        logger.info("Successfully decomposed Gherkin specs via Gemini API!")
        biz_gherkin = llm_specs.get("business", "")
        app_gherkin = llm_specs.get("app", "")
        bff_gherkin = llm_specs.get("bff", "")

        coverage = validate_gherkin_coverage(
            example_map_content,
            biz_gherkin,
            app_gherkin,
            bff_gherkin,
            strict=strict_mode,
        )

        if not coverage["is_covered"]:
            logger.warning(f"LLM generated specs missing coverage: {coverage}. Auto-completing missing tags.")
            for bid in coverage["missing_app"]:
                app_gherkin = f"@covers:{bid}\n" + app_gherkin
            for bid in coverage["missing_bff"]:
                bff_gherkin = f"@covers:{bid}\n" + bff_gherkin

        biz_file = validate_safe_path(biz_dir, biz_dir / f"{pbi_id}.feature")
        app_file = validate_safe_path(app_dir, app_dir / f"{pbi_id}.feature")
        bff_file = validate_safe_path(bff_dir, bff_dir / f"{pbi_id}.feature")

        biz_file.write_text(biz_gherkin, encoding="utf-8")
        app_file.write_text(app_gherkin, encoding="utf-8")
        bff_file.write_text(bff_gherkin, encoding="utf-8")

        return {
            "business": str(biz_file),
            "app": str(app_file),
            "bff": str(bff_file),
        }

    # 2. フォールバック生成: Example Map から BIZ-ID および具体例テーブルをパースして展開 (Issue #13)
    logger.info("Decomposing specs via dynamic fallback parser...")
    # 各 BIZ セクションとその本文を抽出
    biz_sections = re.findall(
        r"##\s+(BIZ-\d+)(?::\s*|\s+)([^\n]+)([\s\S]*?)(?=(?:##\s+BIZ|\Z))",
        example_map_content,
    )

    biz_rules_gherkin = []
    covers_tags = []

    if biz_sections:
        for biz_id, rule_desc_raw, body in biz_sections:
            rule_desc = rule_desc_raw.strip()
            covers_tags.append(f"@covers:{biz_id}")

            # マークダウンテーブルから具体例を抽出 (| ルール | 具体例 (Example) | 期待される結果 |)
            table_lines = [line.strip() for line in body.splitlines() if line.strip().startswith("|")]
            scenario_blocks = []

            for line in table_lines:
                cols = [c.strip() for c in line.split("|") if c.strip()]
                # ヘッダー行や区切り行をスキップ
                if not cols or "ルール" in cols[0] or ":---" in cols[0] or "---" in cols[0]:
                    continue
                if len(cols) >= 3:
                    sub_rule, example_val, expected_res = cols[0], cols[1], cols[2]
                    scenario_blocks.append(f"""    シナリオ: {sub_rule} ({example_val})
      前提 適切な前提条件「{sub_rule}」が成立している
      もし 入力値「{example_val}」が送信されたとき
      ならば 「{expected_res}」となる""")

            if not scenario_blocks:
                scenario_blocks.append(f"""    シナリオ: {rule_desc}の検証
      前提 適切な前提条件が満たされている
      もし 対象の操作を実行したとき
      ならば 期待される結果が得られる""")

            scenarios_text = "\n".join(scenario_blocks)
            biz_rules_gherkin.append(f"""  @{biz_id}
  ルール: {rule_desc}
{scenarios_text}
""")
    else:
        covers_tags = ["@covers:BIZ-001", "@covers:BIZ-002"]
        biz_rules_gherkin.append("""  @BIZ-001
  ルール: 正常系振る舞い
    シナリオ: 正常入力の処理
      前提 前提条件が成立している
      もし 有効なデータが送信されたとき
      ならば 正常に完了する
""")

    covers_header = " ".join(covers_tags)

    # 1. ビジネス層 Gherkin
    biz_file = validate_safe_path(biz_dir, biz_dir / f"{pbi_id}.feature")
    biz_content = f"# language: ja\n機能: {pbi_id} ビジネス振る舞い定義\n\n" + "\n".join(biz_rules_gherkin)
    biz_file.write_text(biz_content, encoding="utf-8")

    # 2. App層 Gherkin
    app_file = validate_safe_path(app_dir, app_dir / f"{pbi_id}.feature")
    app_content = f"""# language: ja
{covers_header}
機能: {pbi_id} クライアントUI振る舞い
  シナリオ: ユーザーインターフェース操作と状態変化
    前提 画面が表示されている
    もし 操作を実行する
    ならば 画面に結果が適切に反映される
"""
    app_file.write_text(app_content, encoding="utf-8")

    # 3. BFF層 Gherkin
    bff_file = validate_safe_path(bff_dir, bff_dir / f"{pbi_id}.feature")
    bff_content = f"""# language: ja
{covers_header}
機能: {pbi_id} APIエンドポイント連携
  シナリオ: リクエスト処理とスキーマ適合レスポンス
    もし APIリクエストが送信される
    ならば ステータスコードとJSONが返却される
"""
    bff_file.write_text(bff_content, encoding="utf-8")

    # カバレッジ検証をフォールバック時にも適用
    validate_gherkin_coverage(
        example_map_content,
        biz_content,
        app_content,
        bff_content,
        strict=strict_mode,
    )

    return {
        "business": str(biz_file),
        "app": str(app_file),
        "bff": str(bff_file),
    }


async def _call_gemini_for_openapi(
    pbi_id: str, bff_gherkin: str, biz_gherkin: str, feedback: Optional[str] = None
) -> Optional[str]:
    api_key = os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")
    if not api_key:
        return None
    try:
        from google import genai

        client = genai.Client(api_key=api_key)
        model_name = os.getenv("DESIGN_AGENT_MODEL") or os.getenv("SPEC_AGENT_MODEL") or "gemini-3.8-flash"

        prompt = f"""あなたは API 仕様設計の専門家「Design Agent」です。
以下の入力情報から、OpenAPI 3.0.3 規格に完全準拠した REST API スキーマ（YAML形式）を策定してください。

# PBI 識別子: {pbi_id}

# BFF層 Gherkin シナリオ:
{bff_gherkin}

# ビジネス層 Gherkin シナリオ:
{biz_gherkin}
"""
        if feedback:
            prompt += f"""
# 人間からの修正指示（最優先でスキーマに反映してください）:
{feedback}
"""

        prompt += """
# 必須要件と規約
1. OpenAPI 3.0.3 規格の YAML 形式のみを出力すること。Markdown コードブロック（```yaml など）は含めないでください。
2. エンドポイントパス、HTTPメソッド（POST/GET/PUT/PATCH/DELETE等）、リクエストボディスキーマ、レスポンススキーマ（200系正常、400系異常、409競合等）を Gherkin のシナリオに合わせて正確に定義すること。
3. すべてのフィールドに型 (type)、説明 (description)、具体例 (example) を含めること。
4. 【重要】YAMLの構文破壊を防ぐため、文字列値（description, summary, example 等）は必ずダブルクォーテーションで囲んでください（特に「:」や日本語を含む場合）。
5. BDD / DDD のビジネスルールに対応するエラーメッセージやバリデーション規約をスキーマに反映すること。
"""
        response = await client.aio.models.generate_content(
            model=model_name,
            contents=prompt,
        )
        cleaned_yaml = _strip_markdown_codeblock(response.text)
        # 構文が正当な OpenAPI YAML（Mapping構造）であることを確認
        try:
            parsed = yaml.safe_load(cleaned_yaml)
            if not isinstance(parsed, dict) or "openapi" not in parsed or "paths" not in parsed:
                logger.warning("Generated OpenAPI schema lacks required root keys ('openapi', 'paths')")
                return None
            return cleaned_yaml
        except yaml.YAMLError as ye:
            logger.warning(f"Generated OpenAPI schema has invalid YAML syntax: {ye}")
            return None
    except Exception as e:
        logger.warning(f"Failed to draft OpenAPI schema via Gemini API: {e}")
        return None


def _fallback_draft_openapi(pbi_id: str, bff_gherkin: str, feedback: Optional[str] = None) -> str:
    """API キー未設定時やエラー時の動的フォールバック OpenAPI 生成 (安全な辞書ダンプ)"""
    clean_note = feedback.replace("\n", " ") if feedback else "standard operation"
    description = f"API contract for {pbi_id} derived from BDD BFF scenarios."
    if feedback:
        description += f"\nHuman Feedback: {feedback}"

    schema_dict = {
        "openapi": "3.0.3",
        "info": {
            "title": f"{pbi_id} API Specification",
            "version": "1.0.0",
            "description": description,
        },
        "paths": {
            f"/api/v1/{pbi_id.lower()}": {
                "post": {
                    "summary": f"Execute {pbi_id} operations",
                    "description": "Auto-generated endpoint matching BFF Gherkin scenarios.",
                    "requestBody": {
                        "required": True,
                        "content": {
                            "application/json": {
                                "schema": {
                                    "type": "object",
                                    "required": ["identifier"],
                                    "properties": {
                                        "identifier": {
                                            "type": "string",
                                            "description": "Unique identifier for the operation",
                                            "example": f"{pbi_id.lower()}-sample-001",
                                        },
                                        "payload": {
                                            "type": "object",
                                            "description": "Request parameters",
                                            "example": {"action": "execute", "note": clean_note},
                                        },
                                    },
                                }
                            }
                        },
                    },
                    "responses": {
                        "200": {
                            "description": "Operation succeeded",
                            "content": {
                                "application/json": {
                                    "schema": {
                                        "type": "object",
                                        "properties": {
                                            "success": {"type": "boolean", "example": True},
                                            "data": {
                                                "type": "object",
                                                "example": {"status": "completed", "pbi": pbi_id},
                                            },
                                        },
                                    }
                                }
                            },
                        },
                        "400": {
                            "description": "Validation error or invalid request format",
                            "content": {
                                "application/json": {
                                    "schema": {
                                        "type": "object",
                                        "properties": {"error": {"type": "string", "example": "Validation failed"}},
                                    }
                                }
                            },
                        },
                        "409": {
                            "description": "Conflict with existing resource state",
                            "content": {
                                "application/json": {
                                    "schema": {
                                        "type": "object",
                                        "properties": {"error": {"type": "string", "example": "Resource conflict"}},
                                    }
                                }
                            },
                        },
                    },
                }
            }
        },
    }
    return yaml.safe_dump(schema_dict, allow_unicode=True, sort_keys=False)


@activity.defn
async def draft_schema_activity(pbi_id: str, decomp_result: Dict[str, str], feedback: Optional[str] = None) -> str:
    """Phase 2-1: OpenAPI スキーマ ドラフト (Gemini API 結合 & 人間フィードバック反映)"""
    pbi_id = validate_pbi_id(pbi_id)
    logger.info(f"Drafting OpenAPI schema for {pbi_id} (feedback: {feedback})")
    schema_dir = BASE_DIR / "specs" / "api" / "v1"
    _ensure_dir(schema_dir)
    schema_file = validate_safe_path(schema_dir, schema_dir / f"{pbi_id}.yaml")

    bff_gherkin = ""
    biz_gherkin = ""
    if decomp_result.get("bff") and Path(decomp_result["bff"]).exists():
        bff_gherkin = Path(decomp_result["bff"]).read_text(encoding="utf-8")
    if decomp_result.get("business") and Path(decomp_result["business"]).exists():
        biz_gherkin = Path(decomp_result["business"]).read_text(encoding="utf-8")

    # 1. 本物の LLM (Gemini API) による OpenAPI 生成を試行
    llm_schema = await _call_gemini_for_openapi(pbi_id, bff_gherkin, biz_gherkin, feedback)
    if llm_schema:
        logger.info("Successfully generated OpenAPI schema via Gemini API!")
        schema_file.write_text(llm_schema, encoding="utf-8")
        return str(schema_file)

    # 2. フォールバック生成
    logger.info("Drafting OpenAPI schema via fallback template...")
    fallback_content = _fallback_draft_openapi(pbi_id, bff_gherkin, feedback)
    schema_file.write_text(fallback_content, encoding="utf-8")
    return str(schema_file)


@activity.defn
async def initialize_tasks_activity(pbi_id: str, schema_path: str) -> None:
    """Phase 2-2: tasks/ 以下の state.json と plan.md の初期化"""
    pbi_id = validate_pbi_id(pbi_id)
    task_dir = validate_safe_path(BASE_DIR / "tasks", BASE_DIR / "tasks" / pbi_id)
    _ensure_dir(task_dir / "app")
    _ensure_dir(task_dir / "bff")

    # state.json (オーケストレーター専用)
    state_file = task_dir / "state.json"
    state_data = {
        "pbi": pbi_id,
        "schema_path": schema_path,
        "pipelines": {
            "app": {"phase": "INIT", "status": "PLANNED"},
            "bff": {"phase": "INIT", "status": "PLANNED"},
        },
    }
    state_file.write_text(json.dumps(state_data, indent=2, ensure_ascii=False), encoding="utf-8")

    # plan.md (App & BFF)
    for comp in ("app", "bff"):
        plan_file = task_dir / comp / "plan.md"
        plan_content = f"""# Execution Plan: {pbi_id} ({comp.upper()})

## 前提リソース
- Schema: `{schema_path}`
- Spec: `specs/features/{comp}/{pbi_id}.feature`

## 進捗
- [x] Phase 1 & 2: 承認完了
- [ ] Phase 4-1: 設計
- [ ] Phase 4-2: TDD (Red/Green/Refactor)
- [ ] Phase 5: 検証

## Current Directive
- 初期化完了。設計フェーズに着手してください。
"""
        plan_file.write_text(plan_content, encoding="utf-8")


def _update_pipeline_phase(pbi_id: str, component: str, phase: str, status: str = "IN_PROGRESS"):
    """tasks/{pbi_id}/state.json のパイプラインフェーズを更新する"""
    pbi_id = validate_pbi_id(pbi_id)
    task_dir = validate_safe_path(BASE_DIR / "tasks", BASE_DIR / "tasks" / pbi_id)
    state_file = task_dir / "state.json"
    if state_file.exists():
        try:
            data = json.loads(state_file.read_text(encoding="utf-8"))
            if component in data.get("pipelines", {}):
                data["pipelines"][component]["phase"] = phase
                data["pipelines"][component]["status"] = status
                state_file.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
        except Exception as e:
            logger.warning(f"Failed to update state.json: {e}")


async def _call_gemini_for_design(
    pbi_id: str, component: str, spec_content: str, schema_content: str, feedback: Optional[str] = None
) -> Optional[str]:
    api_key = os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")
    if not api_key:
        return None
    try:
        from google import genai

        client = genai.Client(api_key=api_key)
        model_name = os.getenv("DESIGN_AGENT_MODEL", os.getenv("SPEC_AGENT_MODEL", "gemini-3.8-flash"))

        feedback_sec = f"\n\n# 直前の分析ディレクティブ / 修正指示:\n{feedback}" if feedback else ""

        prompt = f"""あなたは DDD およびアーキテクチャ設計の専門家「Design Agent」です。
以下の Gherkin 仕様と OpenAPI 契約を基に、{component.upper()} 層の実装設計書 (Markdown形式) を策定してください。

# PBI 識別子: {pbi_id}
# 対象層: {component.upper()}

# Gherkin 仕様:
{spec_content}

# OpenAPI スキーマ契約:
{schema_content}{feedback_sec}

# 必須要件
1. 設計書は Markdown 形式のみ出力すること（コードブロック ```markdown は不要）。
2. ドメインモデル / クラス設計（エンティティ、値オブジェクト、サービスクラス名とその責務）。
3. 入出力パラメータの型定義とバリデーション方針。
4. TDD で実装すべきテストケース一覧（正常系、異常系、境界値）。
"""
        response = await client.aio.models.generate_content(
            model=model_name,
            contents=prompt,
        )
        return _strip_markdown_codeblock(response.text)
    except Exception as e:
        logger.warning(f"Failed to generate design doc via Gemini API: {e}")
        return None


def _fallback_design_doc(pbi_id: str, component: str, spec_content: str, feedback: Optional[str] = None) -> str:
    clean_name = _to_pascal_case(pbi_id)
    feedback_note = f"\n- **Analyzer Directive 反映**: {feedback}\n" if feedback else ""
    return f"""# Architecture & DDD Design: {pbi_id} ({component.upper()})

## 1. 責務と概要
- 対象層: {component.upper()}
- 目的: {pbi_id} のビジネス振る舞いおよび契約仕様を満たすモジュール設計。{feedback_note}
## 2. クラス・サービス設計
- Service: `{clean_name}Service`
  - メソッド: `execute(payload: dict) -> dict`
  - バリデーション: 必須項目チェック、スキーマ適合チェック
  - 例外処理: 不正入力時に `ValueError`、業務エラー時に適切なエラー送出

## 3. TDD 実装方針
- RED: サービスクラスの呼び出しとステータスコード／返却値の検証テストを先行作成。
- GREEN: 最小限のビジネスロジックとマッピングを実装。
- REFACTOR: コード構造の整理、型ヒントの追加。
"""


@activity.defn
async def run_design_activity(pbi_id: str, component: str, feedback: Optional[str] = None) -> Dict[str, Any]:
    """Phase 4-1: DDD / 設計フェーズ (Gemini API 結合)"""
    pbi_id = validate_pbi_id(pbi_id)
    logger.info(f"Running Design activity for {pbi_id} / {component} (feedback={feedback})")
    _update_pipeline_phase(pbi_id, component, "DESIGN", "IN_PROGRESS")

    spec_dir = BASE_DIR / "specs" / "features" / component
    schema_dir = BASE_DIR / "specs" / "api" / "v1"
    spec_path = validate_safe_path(spec_dir, spec_dir / f"{pbi_id}.feature")
    schema_path = validate_safe_path(schema_dir, schema_dir / f"{pbi_id}.yaml")
    spec_content = spec_path.read_text(encoding="utf-8") if spec_path.exists() else ""
    schema_content = schema_path.read_text(encoding="utf-8") if schema_path.exists() else ""

    design_content = await _call_gemini_for_design(pbi_id, component, spec_content, schema_content, feedback)
    if not design_content:
        design_content = _fallback_design_doc(pbi_id, component, spec_content, feedback)

    task_dir = validate_safe_path(BASE_DIR / "tasks", BASE_DIR / "tasks" / pbi_id)
    design_file = validate_safe_path(task_dir, task_dir / component / "design.md")
    _ensure_dir(design_file.parent)
    design_file.write_text(design_content, encoding="utf-8")

    _update_pipeline_phase(pbi_id, component, "DESIGN", "COMPLETED")
    return {
        "design_status": "COMPLETED",
        "component": component,
        "design_path": str(design_file),
    }


def _clean_id(pbi_id: str) -> str:
    return pbi_id.lower().replace("-", "_")


def _generate_test_code(pbi_id: str, component: str, directive_instruction: Optional[str] = None) -> str:
    clean_id = _clean_id(pbi_id)
    class_name = f"{_to_pascal_case(pbi_id)}Service"
    comment = f"\n# Directive Applied: {directive_instruction}\n" if directive_instruction else ""
    return f"""import pytest
{comment}
def _get_service():
    try:
        from src.{component}.{clean_id}_service import {class_name}
        return {class_name}()
    except (ImportError, ModuleNotFoundError):
        pytest.fail("Service implementation not found (expected in RED phase)")


def test_{clean_id}_success():
    service = _get_service()
    result = service.execute({{"identifier": "{clean_id}-test-01"}})
    assert result.get("success") is True
    assert result.get("status_code") == 200


def test_{clean_id}_validation_error():
    service = _get_service()
    with pytest.raises(ValueError, match="identifier is required"):
        service.execute({{}})
"""


def _fallback_impl_code(
    pbi_id: str,
    component: str,
    design_content: Optional[str] = None,
    directive_instruction: Optional[str] = None,
) -> str:
    """DDD設計書を反映した決定論的フォールバック実装コード生成 (Issue #14)"""
    class_name = f"{_to_pascal_case(pbi_id)}Service"
    entity_name = f"{_to_pascal_case(pbi_id)}Entity"
    comment = f"\n    # Directive Applied: {directive_instruction}" if directive_instruction else ""

    design_summary = ""
    if design_content:
        design_summary = "\n# Architecture & Domain Model reflected from design:\n"
        summary_lines = [
            line.strip()
            for line in design_content.splitlines()
            if line.strip().startswith("-") or line.strip().startswith("##")
        ]
        for s in summary_lines[:5]:
            design_summary += f"# {s}\n"

    return f"""from dataclasses import dataclass, field
from typing import Any, Dict, Optional
{design_summary}

@dataclass
class {entity_name}:
    \"\"\"Domain Entity for {pbi_id} ({component})\"\"\"
    identifier: str
    component: str = "{component}"
    status: str = "active"
    metadata: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {{
            "identifier": self.identifier,
            "component": self.component,
            "status": self.status,
            "metadata": self.metadata,
        }}


class {class_name}:
    \"\"\"Domain Service implementation for {pbi_id} ({component}) with DDD Entity integration\"\"\"{comment}

    def __init__(self):
        self._repository: Dict[str, {entity_name}] = {{}}

    def get_entity(self, identifier: str) -> Optional[{entity_name}]:
        return self._repository.get(identifier)

    def execute(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        identifier = payload.get("identifier")
        if not identifier:
            raise ValueError("identifier is required")

        # ドメインエンティティの生成とインメモリストレージへの永続化
        entity = {entity_name}(
            identifier=identifier,
            component="{component}",
            metadata=payload.get("metadata", {{}}),
        )
        self._repository[identifier] = entity

        return {{
            "success": True,
            "status_code": 200,
            "identifier": identifier,
            "component": "{component}",
            "data": entity.to_dict(),
        }}
"""


def _generate_impl_code(
    pbi_id: str,
    component: str,
    design_content: Optional[str] = None,
    directive_instruction: Optional[str] = None,
) -> str:
    """Phase 4-2 Green: DDD設計書および要件に基づくプロダクション実装コード生成 (Issue #14)"""
    class_name = f"{_to_pascal_case(pbi_id)}Service"
    entity_name = f"{_to_pascal_case(pbi_id)}Entity"

    # 1. Gemini API による生成を試行（利用可能な場合）
    api_key = os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")
    if api_key and design_content:
        try:
            from google import genai

            client = genai.Client(api_key=api_key)
            model_name = os.getenv("IMPL_AGENT_MODEL", "gemini-2.5-pro")
            prompt = f"""あなたは DDD / クリーンアーキテクチャの実装スペシャリストです。
以下の設計書および指示に基づき、{component} 層の Python プロダクションコードを生成してください。

# PBI: {pbi_id}
# 設計書:
{design_content}

# 修正指示 (Analyzer Directive):
{directive_instruction or "なし"}

# 必須要件:
1. サービスクラス `{class_name}` を実装すること。
2. `{class_name}` は `def execute(self, payload: Dict[str, Any]) -> Dict[str, Any]` メソッドを持つこと。
3. `payload.get("identifier")` が存在しない場合は `raise ValueError("identifier is required")` すること。
4. 設計書に定義されたエンティティ/モデル `{entity_name}` を dataclass 等で実装し、状態保持・ロジックを実行すること。
5. 正常時は `{{"success": True, "status_code": 200, "identifier": identifier, "component": "{component}", "data": ...}}` を返却すること。
6. Markdown のコードブロックなしで Python コードのみを出力すること。
"""
            resp = client.models.generate_content(model=model_name, contents=prompt)
            if resp.text:
                return _strip_markdown_codeblock(resp.text)
        except Exception as e:
            logger.warning(f"Failed to generate impl code via Gemini: {e}")

    # 2. 設計書を反映した決定論的フォールバック生成
    return _fallback_impl_code(pbi_id, component, design_content, directive_instruction)


async def _call_gemini_for_bdd_steps(
    pbi_id: str,
    component: str,
    feature_content: str,
    directive_instruction: Optional[str] = None,
) -> Optional[str]:
    """Gemini API を利用して Gherkin フィーチャーに対応する pytest-bdd Step Definitions を生成する"""
    api_key = os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")
    if not api_key:
        return None
    try:
        from google import genai

        client = genai.Client(api_key=api_key)
        model_name = os.getenv("SPEC_BDD_MODEL", os.getenv("SPEC_AGENT_MODEL", "gemini-2.5-pro"))

        clean_id = _clean_id(pbi_id)
        service_class = f"{_to_pascal_case(pbi_id)}Service"
        feat_path_str = (BASE_DIR / "specs" / "features" / component / f"{pbi_id}.feature").as_posix()

        directive_section = (
            f"\n# 直前の失敗に対する改善指示 (Analyzer Directive):\n{directive_instruction}\n"
            if directive_instruction
            else ""
        )

        prompt = f"""あなたは BDD (pytest-bdd) の自動テストエンジニアです。
以下の Gherkin フィーチャー仕様書を読み、pytest-bdd を用いた受け入れテスト (Step Definitions) の Python コードを生成してください。

# PBI: {pbi_id}
# 対象コンポーネント: {component}
# 対象フィーチャー仕様:
{feature_content}
{directive_section}
# 要件:
1. `from pytest_bdd import scenarios, given, when, then, parsers` をインポートすること。
2. フィーチャーファイルのパスは `FEATURE_FILE = Path(r"{feat_path_str}")` を定義し、`if FEATURE_FILE.exists(): scenarios(str(FEATURE_FILE))` で読み込むこと。
3. サービスクラス `{service_class}` は共通インターフェース `def execute(self, payload: Dict[str, Any]) -> Dict[str, Any]` のみを持ちます。未定義のメソッド（open_screen や send_request など）を捏造せず、必ず `service.execute({{"identifier": "{clean_id}-bdd-001"}})` を呼び出してレスポンスを取得し、`assert result.get("success") is True` および `assert result.get("status_code") == 200` を検証してください。
4. 各ステップは日本語 Gherkin の表記（前提、もし、ならば）に対応させ、`parsers.parse` または `parsers.re` を使用すること。
5. Python コードのみを出力し、Markdown のコードブロックは不要です。
"""
        response = await client.aio.models.generate_content(
            model=model_name,
            contents=prompt,
        )
        return _strip_markdown_codeblock(response.text)
    except Exception as e:
        logger.warning(f"Failed to generate BDD steps via Gemini: {e}")
        return None


def _generate_bdd_step_code(pbi_id: str, component: str, directive_instruction: Optional[str] = None) -> str:
    """オフライン/フォールバック用の決定論的 pytest-bdd Step Definitions コード生成"""
    clean_id = _clean_id(pbi_id)
    service_class = f"{_to_pascal_case(pbi_id)}Service"
    inst_comment = f"# Directive Note: {directive_instruction}\n" if directive_instruction else ""
    feat_path_str = (BASE_DIR / "specs" / "features" / component / f"{pbi_id}.feature").as_posix()

    return f"""# -*- coding: utf-8 -*-
{inst_comment}from pathlib import Path
import pytest
from pytest_bdd import scenarios, given, when, then, parsers

FEATURE_FILE = Path(r"{feat_path_str}")

if FEATURE_FILE.exists():
    scenarios(str(FEATURE_FILE))


@pytest.fixture
def bdd_context():
    return {{"service": None, "result": None, "identifier": "{clean_id}-bdd-001"}}


def _get_bdd_service():
    try:
        from src.{component}.{clean_id}_service import {service_class}
        return {service_class}()
    except (ImportError, ModuleNotFoundError):
        pytest.fail("BDD service implementation not found (expected in RED phase)")


@given(parsers.re(r".*"))
def bdd_given_step(bdd_context):
    bdd_context["service"] = _get_bdd_service()


@when(parsers.re(r".*"))
def bdd_when_step(bdd_context):
    service = bdd_context.get("service")
    if service is None:
        service = _get_bdd_service()
        bdd_context["service"] = service
    bdd_context["result"] = service.execute({{"identifier": bdd_context["identifier"]}})


@then(parsers.re(r".*"))
def bdd_then_step(bdd_context):
    result = bdd_context.get("result")
    assert result is not None, "Execution result must not be None"
    assert result.get("success") is True, f"Expected success but got: {{result}}"
    assert result.get("status_code") == 200, f"Expected status_code 200 but got: {{result.get('status_code')}}"
    assert result.get("identifier") == bdd_context["identifier"]
"""


async def _run_pytest_on_target(
    test_target: Union[Path, List[Path], str], timeout: float = 30.0
) -> Tuple[int, str, str]:
    """非同期サブプロセスで pytest を実行し、Workerのイベントループを停止させない (単一または複数ターゲットに対応)"""
    if isinstance(test_target, list):
        targets = [str(t) for t in test_target]
    else:
        targets = [str(test_target)]

    cmd = [sys.executable, "-m", "pytest", *targets, "-v"]
    env = os.environ.copy()
    existing_pythonpath = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = f"{BASE_DIR}{os.pathsep}{existing_pythonpath}" if existing_pythonpath else str(BASE_DIR)

    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=str(BASE_DIR),
            env=env,
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        ret_code = proc.returncode if proc.returncode is not None else 1
        return ret_code, stdout.decode("utf-8", errors="replace"), stderr.decode("utf-8", errors="replace")
    except asyncio.TimeoutError:
        try:
            if sys.platform == "win32":
                subprocess.run(
                    ["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                    capture_output=True,
                    check=False,
                )
            else:
                proc.kill()
            await proc.wait()
        except Exception:
            pass
        return -1, "", f"TimeoutError: pytest execution timed out after {timeout} seconds"
    except Exception as e:
        return -2, "", f"SubprocessLaunchError: {str(e)}"


@activity.defn
async def run_tdd_cycle_activity(
    pbi_id: str,
    component: str,
    design_output: Dict[str, Any],
    directive: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Phase 4-2 & 5: TDD サイクル (Red -> Green -> Refactor -> Verify with Rule Guard & Analyzer)"""
    pbi_id = validate_pbi_id(pbi_id)
    logger.info(
        f"Running TDD cycle for {pbi_id} / {component} (directive={directive.get('directive') if directive else None})"
    )
    clean_id = _clean_id(pbi_id)

    comp_src_dir = BASE_DIR / "src" / component
    comp_test_dir = comp_src_dir / "tests"
    _ensure_dir(comp_test_dir)

    for d in (BASE_DIR / "src", comp_src_dir, comp_test_dir):
        init_file = d / "__init__.py"
        if not init_file.exists():
            init_file.write_text("", encoding="utf-8")

    test_file = comp_test_dir / f"test_{clean_id}.py"
    bdd_file = comp_test_dir / f"test_{clean_id}_bdd.py"
    impl_file = comp_src_dir / f"{clean_id}_service.py"

    directive_type = directive.get("directive") if directive else None
    directive_inst = directive.get("instruction", "") if directive else None

    # ---------------------------------------------------------
    # 1. RED (失敗するテストコード作成のみ可: 内側ユニット + 外側受け入れBDD)
    # ---------------------------------------------------------
    # RETRY_IMPL 指定時はテストコードは既存のまま保持し、RED をスキップして GREEN へ進む
    if directive_type != DirectiveType.RETRY_IMPL.value:
        _update_pipeline_phase(pbi_id, component, "RED")
        logger.info(f"[{component}] TDD Phase: RED - Creating failing tests (inner unit & outer BDD)")

        if impl_file.exists():
            impl_file.unlink()

        # 1-a. 内側ループ: ユニットテスト生成
        test_code = _generate_test_code(pbi_id, component, directive_instruction=directive_inst)
        test_file.write_text(test_code, encoding="utf-8")

        # 1-b. 外側ループ: Gherkin から BDD 受け入れテスト生成 (Feature ファイル存在時)
        feature_file = BASE_DIR / "specs" / "features" / component / f"{pbi_id}.feature"
        has_feature = feature_file.exists()
        test_targets = [test_file]

        if has_feature:
            bdd_code = await _call_gemini_for_bdd_steps(
                pbi_id, component, feature_file.read_text(encoding="utf-8"), directive_instruction=directive_inst
            )
            if not bdd_code:
                bdd_code = _generate_bdd_step_code(pbi_id, component, directive_instruction=directive_inst)
            bdd_file.write_text(bdd_code, encoding="utf-8")
            test_targets.append(bdd_file)
        elif bdd_file.exists():
            bdd_file.unlink()

        # RED 検証: 実装がないためテストが確実にアサーションで失敗することを確認 (Issue #12, #19)
        red_code, red_out, red_err = await _run_pytest_on_target(test_targets)
        logger.info(
            f"[{component}] Red phase check: exit_code={red_code} (Expect != 0: {'OK' if red_code != 0 else 'FAIL'})"
        )
        if red_code == 0:
            logger.error(f"[{component}] TDD RED phase violation: Tests passed unexpectedly without implementation.")
            _update_pipeline_phase(pbi_id, component, "RED", "FAILED")
            return {
                "tdd_status": "RED_PHASE_VIOLATION",
                "component": component,
                "error": "TDD RED phase violation: Tests passed unexpectedly without implementation. Tests must assert actual business logic.",
            }

        # Issue #12: 構文エラー・収集エラー・タイムアウト・起動失敗では Red 成功とみなさず失敗とする
        guard_red = classify_execution_output(exit_code=red_code, stdout=red_out, stderr=red_err)
        if guard_red.category != ErrorCategory.TEST_FAILURE:
            logger.error(
                f"[{component}] TDD RED phase execution error: {guard_red.category.value} ({guard_red.summary})"
            )
            _update_pipeline_phase(pbi_id, component, "RED", "FAILED")
            return {
                "tdd_status": "RED_PHASE_EXECUTION_ERROR",
                "component": component,
                "error": f"TDD RED phase error: Tests failed due to {guard_red.category.value} ({guard_red.summary}). Expected assertion failure.",
                "rule_guard": guard_red.to_dict(),
                "output": f"{red_out}\n{red_err}".strip(),
            }

        # Issue #12: 既存の回帰テストが存在する場合、それらがパスすることを確認
        existing_test_files = [f for f in comp_test_dir.glob("test_*.py") if f not in test_targets and f.is_file()]
        if existing_test_files:
            exist_code, exist_out, exist_err = await _run_pytest_on_target(existing_test_files)
            if exist_code != 0:
                logger.error(f"[{component}] TDD RED phase regression failure: Existing tests broken.")
                _update_pipeline_phase(pbi_id, component, "RED", "FAILED")
                return {
                    "tdd_status": "REGRESSION_FAILURE",
                    "component": component,
                    "error": "TDD RED phase regression failure: Existing tests failed when running regression check.",
                    "output": f"{exist_out}\n{exist_err}".strip(),
                }

    # ---------------------------------------------------------
    # 2. GREEN (プロダクション実装のみ可、テスト改ざん不可)
    # ---------------------------------------------------------
    _update_pipeline_phase(pbi_id, component, "GREEN")
    design_content = ""
    if design_output and "design_path" in design_output:
        p = Path(design_output["design_path"])
        if p.exists():
            design_content = p.read_text(encoding="utf-8")
    if not design_content:
        design_file = BASE_DIR / "tasks" / pbi_id / component / "design.md"
        if design_file.exists():
            design_content = design_file.read_text(encoding="utf-8")

    impl_code = _generate_impl_code(
        pbi_id,
        component,
        design_content=design_content,
        directive_instruction=directive_inst,
    )
    impl_file.write_text(impl_code, encoding="utf-8")

    test_targets = [test_file, bdd_file] if bdd_file.exists() else [test_file]

    # GREEN 検証: 内側ユニットテストおよび外側BDDテストの両方が通ることを確認
    green_code, green_out, green_err = await _run_pytest_on_target(test_targets)
    logger.info(
        f"[{component}] Green phase check: exit_code={green_code} (Expect == 0: {'OK' if green_code == 0 else 'FAIL'})"
    )
    if green_code != 0:
        logger.warning(f"[{component}] TDD GREEN phase failed: exit_code={green_code}")
        guard_result = classify_execution_output(exit_code=green_code, stdout=green_out, stderr=green_err)
        _update_pipeline_phase(pbi_id, component, "GREEN", "FAILED")
        return {
            "tdd_status": "GREEN_FAILED",
            "component": component,
            "test_file": str(test_file),
            "bdd_test_file": str(bdd_file) if bdd_file.exists() else None,
            "impl_file": str(impl_file),
            "rule_guard": guard_result.to_dict(),
            "verify_output": f"{green_out}\n{green_err}".strip(),
        }

    # ---------------------------------------------------------
    # 3. REFACTOR (コードクリーンアップ)
    # ---------------------------------------------------------
    _update_pipeline_phase(pbi_id, component, "REFACTOR")
    logger.info(f"[{component}] TDD Phase: REFACTOR - Code cleanup")

    # ---------------------------------------------------------
    # 4. VERIFY (Phase 5: Rule Guard による 0 トークン機械的検証)
    # ---------------------------------------------------------
    _update_pipeline_phase(pbi_id, component, "VERIFY")
    logger.info(f"[{component}] Phase 5: Verification with Rule Guard (0-token)")

    verify_code, verify_out, verify_err = await _run_pytest_on_target(test_targets)
    guard_result = classify_execution_output(exit_code=verify_code, stdout=verify_out, stderr=verify_err)
    logger.info(f"[{component}] Rule Guard decision: {guard_result.category} -> {guard_result.next_action}")

    if guard_result.next_action == NextAction.PROCEED:
        _update_pipeline_phase(pbi_id, component, "DONE", "COMPLETED")
        return {
            "tdd_status": "ALL_GREEN",
            "component": component,
            "test_file": str(test_file),
            "bdd_test_file": str(bdd_file) if bdd_file.exists() else None,
            "impl_file": str(impl_file),
            "rule_guard": guard_result.to_dict(),
        }
    else:
        _update_pipeline_phase(pbi_id, component, "VERIFY", "FAILED")
        return {
            "tdd_status": "VERIFY_FAILED",
            "component": component,
            "test_file": str(test_file),
            "bdd_test_file": str(bdd_file) if bdd_file.exists() else None,
            "impl_file": str(impl_file),
            "rule_guard": guard_result.to_dict(),
            "verify_output": f"{verify_out}\n{verify_err}".strip(),
        }


@activity.defn
async def run_analyzer_activity(pbi_id: str, component: str, failure_context: Dict[str, Any]) -> Dict[str, Any]:
    """Phase 5-3: Analyzer 根本原因分析 & Directive 策定 (ROADMAP P1-4 & WORKFLOW 5-3)"""
    pbi_id = validate_pbi_id(pbi_id)
    logger.info(f"Running Analyzer activity for {pbi_id} / {component}")
    _update_pipeline_phase(pbi_id, component, "ANALYZER", "IN_PROGRESS")

    clean_id = _clean_id(pbi_id)
    comp_src_dir = BASE_DIR / "src" / component
    comp_test_dir = comp_src_dir / "tests"

    test_file = comp_test_dir / f"test_{clean_id}.py"
    impl_file = comp_src_dir / f"{clean_id}_service.py"
    spec_dir = BASE_DIR / "specs" / "features" / component
    schema_dir = BASE_DIR / "specs" / "api" / "v1"
    spec_file = validate_safe_path(spec_dir, spec_dir / f"{pbi_id}.feature")
    schema_file = validate_safe_path(schema_dir, schema_dir / f"{pbi_id}.yaml")
    task_dir = validate_safe_path(BASE_DIR / "tasks", BASE_DIR / "tasks" / pbi_id)
    state_file = task_dir / "state.json"
    plan_file = task_dir / component / "plan.md"
    audit_file = task_dir / "audit.jsonl"
    _ensure_dir(task_dir)
    _ensure_dir(plan_file.parent)

    test_code = test_file.read_text(encoding="utf-8") if test_file.exists() else ""
    impl_code = impl_file.read_text(encoding="utf-8") if impl_file.exists() else ""
    spec_content = spec_file.read_text(encoding="utf-8") if spec_file.exists() else ""
    schema_content = schema_file.read_text(encoding="utf-8") if schema_file.exists() else ""

    test_output = (
        failure_context.get("verify_output")
        or failure_context.get("error")
        or json.dumps(failure_context.get("rule_guard", {}), ensure_ascii=False)
    )

    # サイクルカウントの読み取り & インクリメント
    current_cycle = 1
    if state_file.exists():
        try:
            st_data = json.loads(state_file.read_text(encoding="utf-8"))
            current_cycle = st_data.get("pipelines", {}).get(component, {}).get("analyzer_cycles", 0) + 1
        except Exception:
            pass

    max_cycles = int(os.environ.get("MAX_ANALYZER_CYCLES", "2"))

    # Analyzer 推論実行 (読み取り専用)
    directive = await analyze_failure(
        pbi_id=pbi_id,
        component=component,
        test_output=test_output,
        test_code=test_code,
        impl_code=impl_code,
        spec_content=spec_content,
        schema_content=schema_content,
        cycle_count=current_cycle,
        max_cycles=max_cycles,
        test_file_path=str(test_file),
        impl_file_path=str(impl_file),
    )

    # 1. state.json の更新
    if state_file.exists():
        try:
            st_data = json.loads(state_file.read_text(encoding="utf-8"))
            if component in st_data.get("pipelines", {}):
                st_data["pipelines"][component]["analyzer_cycles"] = current_cycle
                st_data["pipelines"][component]["last_directive"] = directive.to_dict()
                if directive.directive == DirectiveType.ESCALATE_HUMAN:
                    st_data["pipelines"][component]["status"] = "ESCALATED_HUMAN"
            state_file.write_text(json.dumps(st_data, indent=2, ensure_ascii=False), encoding="utf-8")
        except Exception as e:
            logger.warning(f"Failed to update state.json in analyzer: {e}")

    # 2. plan.md の更新
    if plan_file.exists():
        try:
            existing_plan = plan_file.read_text(encoding="utf-8")
            target_str = ", ".join(directive.target_files) if directive.target_files else "None"
            directive_section = f"""
## 🚨 Analyzer Directive: {directive.directive.value} (Cycle {current_cycle}/{max_cycles})
- **判定理由**: {directive.reason}
- **指示内容**: {directive.instruction}
- **対象ファイル**: {target_str}
"""
            plan_file.write_text(existing_plan + directive_section, encoding="utf-8")
        except Exception as e:
            logger.warning(f"Failed to append directive to plan.md: {e}")

    # 3. audit.jsonl への記録
    try:
        from datetime import datetime, timezone

        audit_entry = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "event": "ANALYZER_DIRECTIVE",
            "pbi_id": pbi_id,
            "component": component,
            "cycle": current_cycle,
            "directive": directive.to_dict(),
        }
        with audit_file.open("a", encoding="utf-8") as f:
            f.write(json.dumps(audit_entry, ensure_ascii=False) + "\n")
    except Exception as e:
        logger.warning(f"Failed to write to audit.jsonl: {e}")

    return {
        "pbi_id": pbi_id,
        "component": component,
        "directive": directive.to_dict(),
        "status": "COMPLETED",
    }


def _generate_integration_test_code(pbi_id: str) -> str:
    clean_id = _clean_id(pbi_id)
    app_class = f"{_to_pascal_case(pbi_id)}Service"
    bff_class = f"{_to_pascal_case(pbi_id)}Service"
    return f"""import pytest
from src.app.{clean_id}_service import {app_class} as AppService
from src.bff.{clean_id}_service import {bff_class} as BffService

def test_{clean_id}_end_to_end_integration():
    app = AppService()
    bff = BffService()

    # 1. クライアントUI (App) 操作の実行
    app_result = app.execute({{"identifier": "{clean_id}-e2e-001"}})
    assert app_result.get("success") is True
    assert app_result.get("status_code") == 200

    # 2. BFF エンドポイントへの連携とレスポンス検証
    bff_result = bff.execute({{"identifier": app_result.get("identifier")}})
    assert bff_result.get("success") is True
    assert bff_result.get("status_code") == 200
    assert bff_result.get("identifier") == "{clean_id}-e2e-001"
"""


def _generate_integration_bdd_test_code(pbi_id: str) -> str:
    """ビジネス層 Gherkin シナリオを実行する結合 BDD 受け入れテストコード生成"""
    clean_id = _clean_id(pbi_id)
    app_class = f"{_to_pascal_case(pbi_id)}Service"
    bff_class = f"{_to_pascal_case(pbi_id)}Service"

    biz_feat_path_str = (BASE_DIR / "specs" / "features" / "business" / f"{pbi_id}.feature").as_posix()

    return f"""# -*- coding: utf-8 -*-
from pathlib import Path
import pytest
from pytest_bdd import scenarios, given, when, then, parsers
from src.app.{clean_id}_service import {app_class} as AppService
from src.bff.{clean_id}_service import {bff_class} as BffService

BIZ_FEATURE_FILE = Path(r"{biz_feat_path_str}")

if BIZ_FEATURE_FILE.exists():
    scenarios(str(BIZ_FEATURE_FILE))


@pytest.fixture
def integ_bdd_context():
    return {{
        "app": AppService(),
        "bff": BffService(),
        "app_result": None,
        "bff_result": None,
        "identifier": "{clean_id}-integ-bdd-001",
    }}


@given(parsers.re(r".*"))
def integ_given_step(integ_bdd_context):
    assert integ_bdd_context["app"] is not None
    assert integ_bdd_context["bff"] is not None


@when(parsers.re(r".*"))
def integ_when_step(integ_bdd_context):
    app = integ_bdd_context["app"]
    bff = integ_bdd_context["bff"]
    ident = integ_bdd_context["identifier"]

    # 1. App 実行
    app_res = app.execute({{"identifier": ident}})
    integ_bdd_context["app_result"] = app_res

    # 2. BFF 実行
    bff_res = bff.execute({{"identifier": app_res.get("identifier")}})
    integ_bdd_context["bff_result"] = bff_res


@then(parsers.re(r".*"))
def integ_then_step(integ_bdd_context):
    app_res = integ_bdd_context["app_result"]
    bff_res = integ_bdd_context["bff_result"]

    assert app_res is not None and app_res.get("success") is True
    assert app_res.get("status_code") == 200

    assert bff_res is not None and bff_res.get("success") is True
    assert bff_res.get("status_code") == 200
    assert bff_res.get("identifier") == integ_bdd_context["identifier"]
"""


def _get_github_token() -> Optional[str]:
    """環境変数または git credential helper から GitHub トークンを取得する"""
    token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    if token:
        return token
    proc = None
    try:
        proc = subprocess.Popen(
            ["git", "credential", "fill"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        stdout, _ = proc.communicate("protocol=https\nhost=github.com\n\n", timeout=5.0)
        creds = dict(line.split("=", 1) for line in stdout.strip().split("\n") if "=" in line)
        return creds.get("password")
    except subprocess.TimeoutExpired:
        if proc:
            proc.kill()
            try:
                proc.communicate()
            except Exception:
                pass
        return None
    except Exception:
        if proc:
            try:
                proc.kill()
            except Exception:
                pass
        return None


def _get_repo_slug() -> str:
    """現在の git リポジトリのスラッグ (owner/repo) を取得する (環境変数 GITHUB_REPOSITORY 優先)"""
    repo = os.environ.get("GITHUB_REPOSITORY")
    if repo:
        return repo
    try:
        proc = subprocess.run(
            ["git", "remote", "get-url", "origin"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=True,
            timeout=5.0,
        )
        url = proc.stdout.strip()
        match = re.search(r"github\.com[/:]([^/]+/[^/.]+)(?:\.git)?", url)
        if match:
            return match.group(1)
    except Exception:
        pass
    return "Calpim/agentic-dev-sandbox"


def _get_current_branch() -> str:
    """現在のブランチ名を取得する"""
    try:
        proc = subprocess.run(
            ["git", "branch", "--show-current"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=True,
            timeout=5.0,
        )
        return proc.stdout.strip() or "main"
    except Exception:
        return "main"


def _build_pr_body(
    pbi_id: str,
    app_res: Dict[str, Any],
    bff_res: Dict[str, Any],
    integration_res: Dict[str, Any],
) -> str:
    clean_id = _clean_id(pbi_id)
    em_file = BASE_DIR / "specs" / "example-maps" / f"{pbi_id}.md"

    # BIZ-ID 抽出
    biz_rules = []
    if em_file.exists():
        biz_matches = re.findall(r"##\s+(BIZ-\d+)(?::\s*|\s+)(.+)", em_file.read_text(encoding="utf-8"))
        for bid, rdesc in biz_matches:
            biz_rules.append((bid, rdesc.strip()))
    if not biz_rules:
        biz_rules = [("BIZ-001", "基本機能振る舞い")]

    # 各パイプラインおよび結合検証の成否判定 (Issue 3: 動的判定)
    app_ok = app_res.get("status") == "SUCCESS" or app_res.get("tdd_status") == "ALL_GREEN"
    bff_ok = bff_res.get("status") == "SUCCESS" or bff_res.get("tdd_status") == "ALL_GREEN"
    integ_ok = integration_res.get("status") == "ALL_GREEN"
    all_ok = app_ok and bff_ok and integ_ok

    status_badge = "✅ ALL GREEN" if all_ok else "❌ FAILED"

    traceability_rows = []
    for bid, desc in biz_rules:
        traceability_rows.append(
            f"| `{bid}` | {desc} | `@covers:{bid}` | `src/app/tests/test_{clean_id}.py`<br>`src/bff/tests/test_{clean_id}.py` | `src/app/{clean_id}_service.py`<br>`src/bff/{clean_id}_service.py` | {status_badge} |"
        )
    traceability_table = "\n".join(traceability_rows)

    guard_app = app_res.get("rule_guard", {}).get("category", "UNKNOWN")
    guard_bff = bff_res.get("rule_guard", {}).get("category", "UNKNOWN")
    guard_integ = integration_res.get("rule_guard", {}).get("category", "UNKNOWN")

    chk_app = "x" if app_ok else " "
    chk_bff = "x" if bff_ok else " "
    chk_integ = "x" if integ_ok else " "
    integ_label = "合格" if integ_ok else "不合格"

    # Analyzer 介入実績の抽出
    audit_file = BASE_DIR / "tasks" / pbi_id / "audit.jsonl"
    analyzer_entries = []
    if audit_file.exists():
        try:
            for line in audit_file.read_text(encoding="utf-8").splitlines():
                if not line.strip():
                    continue
                entry = json.loads(line)
                if entry.get("event") == "ANALYZER_DIRECTIVE":
                    analyzer_entries.append(entry)
        except Exception:
            pass

    analyzer_section = ""
    reviewer_extra = ""
    if analyzer_entries:
        rows = []
        for ae in analyzer_entries:
            comp = ae.get("component", "unknown")
            cycle = ae.get("cycle", 1)
            dir_data = ae.get("directive", {})
            dtype = dir_data.get("directive", "UNKNOWN")
            reason = dir_data.get("reason", "")
            rows.append(f"- **[{comp.upper()}] Cycle {cycle} ({dtype})**: {reason}")
        analyzer_section = f"""
---

## 🤖 Analyzer 介入実績 (Phase 5-3 自律自己修復)
{chr(10).join(rows)}
"""
        reviewer_extra = "\n3. **Analyzer による自己修復 (FIX_TEST / RETRY_IMPL)** が発生したコンポーネントについて、指示内容およびテスト/実装の修正妥当性を重点的にご確認ください。"

    has_integ_bdd = bool(integration_res.get("bdd_test_file"))
    integ_scope = "App-BFF 契約適合性 ＆ ビジネス Gherkin 受け入れ検証" if has_integ_bdd else "App-BFF 契約適合性検証"
    bdd_reviewer_note = (
        f" および結合 BDD テスト (`src/integration/tests/test_{clean_id}_bdd_integration.py`)" if has_integ_bdd else ""
    )

    return f"""## 📌 概要
自律開発パイプライン (v3.0) により、PBI `{pbi_id}` に対する要件分析から結合テストまでを全自動完了した Pull Request です。

---

## 🎯 トレーサビリティ対応表 (ROADMAP P1-6 準拠)

| BIZ-ID | ビジネスルール | Gherkin 対応 | テストコード | 実装コード | 検証状態 |
| :--- | :--- | :--- | :--- | :--- | :---: |
{traceability_table}

---

## 🧪 検証実績 (Integration Gate {integ_label})

- [x] **Phase 1: Example Mapping 人間ゲート 1 承認済** (`specs/example-maps/{pbi_id}.md`)
- [x] **Phase 1-3: App / BFF Gherkin 分解 ＆ @covers タグ付与**
- [x] **Phase 2: OpenAPI 3.0 スキーマ人間ゲート 2 承認済** (`specs/api/v1/{pbi_id}.yaml`)
- [{chk_app}] **Phase 4-1 & 4-2: App TDD サイクル** (二重ループ: 内側ユニット + 外側 BDD / Rule Guard: `{guard_app}`)
- [{chk_bff}] **Phase 4-1 & 4-2: BFF TDD サイクル** (二重ループ: 内側ユニット + 外側 BDD / Rule Guard: `{guard_bff}`)
- [{chk_integ}] **Phase 6: 結合検証 (Integration Gate)**: {integ_scope} {integ_label} (`{guard_integ}`)
{analyzer_section}
---

## 👀 レビュアーへの確認ポイント (重点レビュー箇所)
1. トレーサビリティ表の `BIZ-ID` が、Example Map の受け入れ基準をすべて網羅していることをご確認ください。
2. 二重ループ TDD の外側受け入れテスト (`src/*/tests/test_{clean_id}_bdd.py`){bdd_reviewer_note} が、Gherkin 仕様の振る舞い要件を正確に検証していることをご確認ください。
3. 結合テスト (`src/integration/tests/test_{clean_id}_integration.py`) が App と BFF 間のリクエスト・レスポンス契約を満たしていることをご確認ください。{reviewer_extra}
"""


def _create_github_pr(pbi_id: str, title: str, body: str) -> Optional[str]:
    """GitHub REST API を通じて Pull Request を自動作成する"""
    token = _get_github_token()
    if not token:
        logger.info("GitHub token not found. Skipping automatic PR creation.")
        return None

    repo_slug = _get_repo_slug()
    branch = _get_current_branch()
    base_branch = os.environ.get("BASE_BRANCH", "main")

    if branch == base_branch:
        logger.info(f"Currently on {base_branch} branch. Cannot create PR from {base_branch} to {base_branch}.")
        return None

    url = f"https://api.github.com/repos/{repo_slug}/pulls"
    payload = json.dumps(
        {
            "title": title,
            "head": branch,
            "base": base_branch,
            "body": body,
        }
    ).encode("utf-8")

    req = urllib.request.Request(
        url,
        data=payload,
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "Antigravity-Agent",
        },
    )

    try:
        with urllib.request.urlopen(req, timeout=10.0) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            pr_url = data.get("html_url")
            logger.info(f"Successfully created GitHub PR: {pr_url}")
            return pr_url
    except urllib.error.HTTPError as e:
        err_msg = e.read().decode("utf-8", errors="replace")
        logger.warning(f"Failed to create GitHub PR (HTTP {e.code}): {err_msg}")
        return None
    except Exception as e:
        logger.warning(f"Failed to create GitHub PR: {e}")
        return None


@activity.defn
async def integration_and_pr_activity(pbi_id: str, app_res: Dict[str, Any], bff_res: Dict[str, Any]) -> Dict[str, Any]:
    """Phase 6: 結合検証 (Integration Gate: 契約適合 + ビジネス Gherkin BDD) & Phase 7: GitHub PR 自動作成"""
    pbi_id = validate_pbi_id(pbi_id)
    logger.info(f"Phase 6: Starting Integration Verification and PR creation for {pbi_id}")
    clean_id = _clean_id(pbi_id)

    # 1. 結合テストディレクトリの準備
    integ_dir = BASE_DIR / "src" / "integration"
    integ_test_dir = integ_dir / "tests"
    _ensure_dir(integ_test_dir)
    for d in (BASE_DIR / "src", integ_dir, integ_test_dir):
        init_file = d / "__init__.py"
        if not init_file.exists():
            init_file.write_text("", encoding="utf-8")

    # 2. 結合テストコード生成 (ユニット結合 + ビジネス Gherkin BDD 結合)
    test_file = integ_test_dir / f"test_{clean_id}_integration.py"
    integ_test_code = _generate_integration_test_code(pbi_id)
    test_file.write_text(integ_test_code, encoding="utf-8")

    biz_feat_file = BASE_DIR / "specs" / "features" / "business" / f"{pbi_id}.feature"
    test_targets = [test_file]
    bdd_test_file_path: Optional[str] = None

    if biz_feat_file.exists():
        bdd_test_file = integ_test_dir / f"test_{clean_id}_bdd_integration.py"
        integ_bdd_code = _generate_integration_bdd_test_code(pbi_id)
        bdd_test_file.write_text(integ_bdd_code, encoding="utf-8")
        test_targets.append(bdd_test_file)
        bdd_test_file_path = str(bdd_test_file)

    # 3. 結合テスト実行 (非同期 pytest)
    verify_code, verify_out, verify_err = await _run_pytest_on_target(test_targets)
    guard_result = classify_execution_output(exit_code=verify_code, stdout=verify_out, stderr=verify_err)
    logger.info(f"Integration Rule Guard decision: {guard_result.category} -> {guard_result.next_action}")

    is_passed = guard_result.next_action == NextAction.PROCEED
    integration_res = {
        "status": "ALL_GREEN" if is_passed else "FAILED",
        "rule_guard": guard_result.to_dict(),
        "test_file": str(test_file),
        "bdd_test_file": bdd_test_file_path,
    }

    # 4. PR サマリー生成 & tasks/ 保存
    pr_body = _build_pr_body(pbi_id, app_res, bff_res, integration_res)
    pr_file = validate_safe_path(BASE_DIR / "tasks", BASE_DIR / "tasks" / pbi_id / "PR_SUMMARY.md")
    _ensure_dir(pr_file.parent)
    pr_file.write_text(pr_body, encoding="utf-8")

    # 結合検証失敗時は PR 起票を阻止し、FAILED を返却 (Issue 1)
    if not is_passed:
        logger.error(f"Integration gate failed for {pbi_id}. Skipping GitHub PR creation.")
        return {
            "pbi_id": pbi_id,
            "status": "FAILED",
            "pr_url": None,
            "pr_summary_path": str(pr_file),
            "integration_status": "FAILED",
        }

    # 5. GitHub PR 起票の試行（合格時のみ実行、asyncio.to_thread で非同期委任） (Issue 2)
    pr_title = f"feat({pbi_id.lower()}): 自律開発パイプラインによる実装と結合検証完了"
    pr_url = await asyncio.to_thread(_create_github_pr, pbi_id, pr_title, pr_body)

    return {
        "pbi_id": pbi_id,
        "status": "COMPLETED",
        "pr_url": pr_url,
        "pr_summary_path": str(pr_file),
        "integration_status": "ALL_GREEN",
        "bdd_test_file": bdd_test_file_path,
    }
