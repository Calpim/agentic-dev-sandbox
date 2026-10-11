import os
import sys
import asyncio
import subprocess
import json
import logging
import re
import yaml
import urllib.request
import urllib.error
from pathlib import Path
from typing import Dict, Any, Optional, Tuple

def _to_pascal_case(identifier: str) -> str:
    """ハイフンやアンダースコア区切りの文字列を PascalCase / CamelCase に変換する"""
    return re.sub(r"[^a-zA-Z0-9]+", " ", identifier).title().replace(" ", "")

from temporalio import activity

from dotenv import load_dotenv

from orchestrator.guards.rule_guard import (
    classify_execution_output,
    ErrorCategory,
    NextAction,
)

load_dotenv()
logger = logging.getLogger("orchestrator.activities")

BASE_DIR = Path(__file__).resolve().parent.parent

def _ensure_dir(p: Path):
    p.mkdir(parents=True, exist_ok=True)

def _strip_markdown_codeblock(text: str) -> str:
    """LLM が出力した Markdown コードブロック (```markdown ... ```) を除去する"""
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
    logger.info(f"Generating Example Map for {pbi_id} (feedback: {feedback})")
    
    target_dir = BASE_DIR / "specs" / "example-maps"
    _ensure_dir(target_dir)
    target_file = target_dir / f"{pbi_id}.md"

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
    logger.info(f"Decomposing specs for {pbi_id} from {example_map_path}")
    
    biz_dir = BASE_DIR / "specs" / "features" / "business"
    app_dir = BASE_DIR / "specs" / "features" / "app"
    bff_dir = BASE_DIR / "specs" / "features" / "bff"
    for d in (biz_dir, app_dir, bff_dir):
        _ensure_dir(d)

    example_map_content = Path(example_map_path).read_text(encoding="utf-8")

    # 1. 本物の LLM (Gemini API) による仕様分解を試行
    llm_specs = await _call_gemini_for_decomposition(pbi_id, example_map_content)
    if llm_specs:
        logger.info("Successfully decomposed Gherkin specs via Gemini API!")
        biz_file = biz_dir / f"{pbi_id}.feature"
        app_file = app_dir / f"{pbi_id}.feature"
        bff_file = bff_dir / f"{pbi_id}.feature"
        
        biz_file.write_text(llm_specs["business"], encoding="utf-8")
        app_file.write_text(llm_specs["app"], encoding="utf-8")
        bff_file.write_text(llm_specs["bff"], encoding="utf-8")
        
        return {
            "business": str(biz_file),
            "app": str(app_file),
            "bff": str(bff_file),
        }

    # 2. フォールバック生成: Example Map から BIZ-ID を動的に抽出して展開
    logger.info("Decomposing specs via dynamic fallback parser...")
    biz_matches = re.findall(r"##\s+(BIZ-\d+)(?::\s*|\s+)(.+)", example_map_content)
    
    biz_rules_gherkin = []
    covers_tags = []
    
    if biz_matches:
        for biz_id, rule_desc in biz_matches:
            covers_tags.append(f"@covers:{biz_id}")
            biz_rules_gherkin.append(f"""  @{biz_id}
  ルール: {rule_desc.strip()}
    シナリオ: {rule_desc.strip()}の検証
      前提 適切な前提条件が満たされている
      もし 対象の操作を実行したとき
      ならば 期待される結果が得られる
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
    biz_file = biz_dir / f"{pbi_id}.feature"
    biz_content = f"# language: ja\n機能: {pbi_id} ビジネス振る舞い定義\n\n" + "\n".join(biz_rules_gherkin)
    biz_file.write_text(biz_content, encoding="utf-8")

    # 2. App層 Gherkin
    app_file = app_dir / f"{pbi_id}.feature"
    app_file.write_text(f"""# language: ja
{covers_header}
機能: {pbi_id} クライアントUI振る舞い
  シナリオ: ユーザーインターフェース操作と状態変化
    前提 画面が表示されている
    もし 操作を実行する
    ならば 画面に結果が適切に反映される
""", encoding="utf-8")

    # 3. BFF層 Gherkin
    bff_file = bff_dir / f"{pbi_id}.feature"
    bff_file.write_text(f"""# language: ja
{covers_header}
機能: {pbi_id} APIエンドポイント連携
  シナリオ: リクエスト処理とスキーマ適合レスポンス
    もし APIリクエストが送信される
    ならば ステータスコードとJSONが返却される
""", encoding="utf-8")

    return {
        "business": str(biz_file),
        "app": str(app_file),
        "bff": str(bff_file),
    }

async def _call_gemini_for_openapi(
    pbi_id: str,
    bff_gherkin: str,
    biz_gherkin: str,
    feedback: Optional[str] = None
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
                                            "data": {"type": "object", "example": {"status": "completed", "pbi": pbi_id}},
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
    logger.info(f"Drafting OpenAPI schema for {pbi_id} (feedback: {feedback})")
    schema_dir = BASE_DIR / "specs" / "api" / "v1"
    _ensure_dir(schema_dir)
    schema_file = schema_dir / f"{pbi_id}.yaml"

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
    task_dir = BASE_DIR / "tasks" / pbi_id
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
        }
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
    state_file = BASE_DIR / "tasks" / pbi_id / "state.json"
    if state_file.exists():
        try:
            data = json.loads(state_file.read_text(encoding="utf-8"))
            if component in data.get("pipelines", {}):
                data["pipelines"][component]["phase"] = phase
                data["pipelines"][component]["status"] = status
                state_file.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
        except Exception as e:
            logger.warning(f"Failed to update state.json: {e}")

async def _call_gemini_for_design(pbi_id: str, component: str, spec_content: str, schema_content: str) -> Optional[str]:
    api_key = os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")
    if not api_key:
        return None
    try:
        from google import genai
        client = genai.Client(api_key=api_key)
        model_name = os.getenv("DESIGN_AGENT_MODEL", os.getenv("SPEC_AGENT_MODEL", "gemini-3.8-flash"))
        
        prompt = f"""あなたは DDD およびアーキテクチャ設計の専門家「Design Agent」です。
以下の Gherkin 仕様と OpenAPI 契約を基に、{component.upper()} 層の実装設計書 (Markdown形式) を策定してください。

# PBI 識別子: {pbi_id}
# 対象層: {component.upper()}

# Gherkin 仕様:
{spec_content}

# OpenAPI スキーマ契約:
{schema_content}

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

def _fallback_design_doc(pbi_id: str, component: str, spec_content: str) -> str:
    clean_name = _to_pascal_case(pbi_id)
    return f"""# Architecture & DDD Design: {pbi_id} ({component.upper()})

## 1. 責務と概要
- 対象層: {component.upper()}
- 目的: {pbi_id} のビジネス振る舞いおよび契約仕様を満たすモジュール設計。

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
async def run_design_activity(pbi_id: str, component: str) -> Dict[str, Any]:
    """Phase 4-1: DDD / 設計フェーズ (Gemini API 結合)"""
    logger.info(f"Running Design activity for {pbi_id} / {component}")
    _update_pipeline_phase(pbi_id, component, "DESIGN", "IN_PROGRESS")

    spec_path = BASE_DIR / "specs" / "features" / component / f"{pbi_id}.feature"
    schema_path = BASE_DIR / "specs" / "api" / "v1" / f"{pbi_id}.yaml"
    spec_content = spec_path.read_text(encoding="utf-8") if spec_path.exists() else ""
    schema_content = schema_path.read_text(encoding="utf-8") if schema_path.exists() else ""

    design_content = await _call_gemini_for_design(pbi_id, component, spec_content, schema_content)
    if not design_content:
        design_content = _fallback_design_doc(pbi_id, component, spec_content)

    design_file = BASE_DIR / "tasks" / pbi_id / component / "design.md"
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

def _generate_test_code(pbi_id: str, component: str) -> str:
    clean_id = _clean_id(pbi_id)
    class_name = f"{_to_pascal_case(pbi_id)}Service"
    return f"""import pytest
from src.{component}.{clean_id}_service import {class_name}

def test_{clean_id}_success():
    service = {class_name}()
    result = service.execute({{"identifier": "{clean_id}-test-01"}})
    assert result.get("success") is True
    assert result.get("status_code") == 200

def test_{clean_id}_validation_error():
    service = {class_name}()
    with pytest.raises(ValueError, match="identifier is required"):
        service.execute({{}})
"""

def _generate_impl_code(pbi_id: str, component: str) -> str:
    clean_id = _clean_id(pbi_id)
    class_name = f"{_to_pascal_case(pbi_id)}Service"
    return f"""from typing import Dict, Any

class {class_name}:
    \"\"\"Auto-generated service implementation for {pbi_id} ({component})\"\"\"
    
    def execute(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        identifier = payload.get("identifier")
        if not identifier:
            raise ValueError("identifier is required")
        
        return {{
            "success": True,
            "status_code": 200,
            "identifier": identifier,
            "component": "{component}",
        }}
"""

async def _run_pytest_on_target(test_file: Path, timeout: float = 30.0) -> Tuple[int, str, str]:
    """非同期サブプロセスで pytest を実行し、Workerのイベントループを停止させない"""
    cmd = [sys.executable, "-m", "pytest", str(test_file), "-v"]
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
        return proc.returncode, stdout.decode("utf-8", errors="replace"), stderr.decode("utf-8", errors="replace")
    except asyncio.TimeoutError:
        try:
            proc.kill()
            await proc.wait()
        except Exception:
            pass
        return -1, "", f"TimeoutError: pytest execution timed out after {timeout} seconds"

@activity.defn
async def run_tdd_cycle_activity(pbi_id: str, component: str, design_output: Dict[str, Any]) -> Dict[str, Any]:
    """Phase 4-2 & 5: TDD サイクル (Red -> Green -> Refactor -> Verify with Rule Guard)"""
    logger.info(f"Running TDD cycle for {pbi_id} / {component}")
    clean_id = _clean_id(pbi_id)
    
    comp_src_dir = BASE_DIR / "src" / component
    comp_test_dir = comp_src_dir / "tests"
    _ensure_dir(comp_test_dir)
    
    # Python パッケージ初期化 (__init__.py)
    for d in (BASE_DIR / "src", comp_src_dir, comp_test_dir):
        init_file = d / "__init__.py"
        if not init_file.exists():
            init_file.write_text("", encoding="utf-8")

    test_file = comp_test_dir / f"test_{clean_id}.py"
    impl_file = comp_src_dir / f"{clean_id}_service.py"

    # ---------------------------------------------------------
    # 1. RED (失敗するテストコード作成のみ可)
    # ---------------------------------------------------------
    _update_pipeline_phase(pbi_id, component, "RED")
    logger.info(f"[{component}] TDD Phase: RED - Creating failing tests at {test_file}")
    
    if impl_file.exists():
        impl_file.unlink()

    test_code = _generate_test_code(pbi_id, component)
    test_file.write_text(test_code, encoding="utf-8")

    # RED 検証: 実装がないためテストが確実に失敗することを確認 (非同期 & 厳格検証)
    red_code, red_out, red_err = await _run_pytest_on_target(test_file)
    logger.info(f"[{component}] Red phase check: exit_code={red_code} (Expect != 0: {'OK' if red_code != 0 else 'FAIL'})")
    if red_code == 0:
        logger.error(f"[{component}] TDD RED phase violation: Tests passed unexpectedly without implementation.")
        _update_pipeline_phase(pbi_id, component, "RED", "FAILED")
        return {
            "tdd_status": "RED_PHASE_VIOLATION",
            "component": component,
            "error": "TDD RED phase violation: Tests passed unexpectedly without implementation. Tests must assert actual business logic.",
        }

    # ---------------------------------------------------------
    # 2. GREEN (プロダクション実装のみ可、テスト改ざん不可)
    # ---------------------------------------------------------
    _update_pipeline_phase(pbi_id, component, "GREEN")
    logger.info(f"[{component}] TDD Phase: GREEN - Implementing solution at {impl_file}")

    impl_code = _generate_impl_code(pbi_id, component)
    impl_file.write_text(impl_code, encoding="utf-8")

    # GREEN 検証: テストが通ることを確認 (非同期 & 早期ハンドリング)
    green_code, green_out, green_err = await _run_pytest_on_target(test_file)
    logger.info(f"[{component}] Green phase check: exit_code={green_code} (Expect == 0: {'OK' if green_code == 0 else 'FAIL'})")
    if green_code != 0:
        logger.warning(f"[{component}] TDD GREEN phase failed: exit_code={green_code}")
        guard_result = classify_execution_output(exit_code=green_code, stdout=green_out, stderr=green_err)
        _update_pipeline_phase(pbi_id, component, "GREEN", "FAILED")
        return {
            "tdd_status": "GREEN_FAILED",
            "component": component,
            "test_file": str(test_file),
            "impl_file": str(impl_file),
            "rule_guard": guard_result.to_dict(),
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

    verify_code, verify_out, verify_err = await _run_pytest_on_target(test_file)
    guard_result = classify_execution_output(exit_code=verify_code, stdout=verify_out, stderr=verify_err)
    logger.info(f"[{component}] Rule Guard decision: {guard_result.category} -> {guard_result.next_action}")

    _update_pipeline_phase(pbi_id, component, "DONE", "COMPLETED")

    return {
        "tdd_status": "ALL_GREEN" if guard_result.next_action == NextAction.PROCEED else "FAILED",
        "component": component,
        "test_file": str(test_file),
        "impl_file": str(impl_file),
        "rule_guard": guard_result.to_dict(),
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
    app_ok = (app_res.get("status") == "SUCCESS" or app_res.get("tdd_status") == "ALL_GREEN")
    bff_ok = (bff_res.get("status") == "SUCCESS" or bff_res.get("tdd_status") == "ALL_GREEN")
    integ_ok = (integration_res.get("status") == "ALL_GREEN")
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
- [{chk_app}] **Phase 4-1 & 4-2: App TDD サイクル** (Rule Guard: `{guard_app}`)
- [{chk_bff}] **Phase 4-1 & 4-2: BFF TDD サイクル** (Rule Guard: `{guard_bff}`)
- [{chk_integ}] **Phase 6: 結合検証 (Integration Gate)**: App-BFF エンドツーエンド契約適合性検証 {integ_label} (`{guard_integ}`)

---

## 👀 レビュアーへの確認ポイント
1. トレーサビリティ表の `BIZ-ID` が、Example Map の受け入れ基準をすべて網羅していることをご確認ください。
2. 結合テスト (`src/integration/tests/test_{clean_id}_integration.py`) が App と BFF 間のリクエスト・レスポンス契約を満たしていることをご確認ください。
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
    payload = json.dumps({
        "title": title,
        "head": branch,
        "base": base_branch,
        "body": body,
    }).encode("utf-8")

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
    """Phase 6: 結合検証 (Integration Gate) & Phase 7: GitHub PR 自動作成"""
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

    # 2. 結合テストコード生成
    test_file = integ_test_dir / f"test_{clean_id}_integration.py"
    integ_test_code = _generate_integration_test_code(pbi_id)
    test_file.write_text(integ_test_code, encoding="utf-8")

    # 3. 結合テスト実行 (非同期 pytest)
    verify_code, verify_out, verify_err = await _run_pytest_on_target(test_file)
    guard_result = classify_execution_output(exit_code=verify_code, stdout=verify_out, stderr=verify_err)
    logger.info(f"Integration Rule Guard decision: {guard_result.category} -> {guard_result.next_action}")

    is_passed = (guard_result.next_action == NextAction.PROCEED)
    integration_res = {
        "status": "ALL_GREEN" if is_passed else "FAILED",
        "rule_guard": guard_result.to_dict(),
        "test_file": str(test_file),
    }

    # 4. PR サマリー生成 & tasks/ 保存
    pr_body = _build_pr_body(pbi_id, app_res, bff_res, integration_res)
    pr_file = BASE_DIR / "tasks" / pbi_id / "PR_SUMMARY.md"
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
    }
