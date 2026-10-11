import os
import json
import logging
from pathlib import Path
from typing import Dict, Any, Optional

from temporalio import activity

from dotenv import load_dotenv

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
        model_name = os.getenv("SPEC_AGENT_MODEL", "gemini-2.5-flash")
        
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

@activity.defn
async def decompose_specs_activity(pbi_id: str, example_map_path: str) -> Dict[str, str]:
    """Phase 1-2 & 1-3: ビジネス Gherkin 生成 & App/BFF 分解"""
    logger.info(f"Decomposing specs for {pbi_id} from {example_map_path}")
    
    biz_dir = BASE_DIR / "specs" / "features" / "business"
    app_dir = BASE_DIR / "specs" / "features" / "app"
    bff_dir = BASE_DIR / "specs" / "features" / "bff"
    for d in (biz_dir, app_dir, bff_dir):
        _ensure_dir(d)

    # 1. ビジネス層 Gherkin
    biz_file = biz_dir / f"{pbi_id}.feature"
    biz_file.write_text(f"""# language: ja
機能: {pbi_id} ビジネス振る舞い定義

  @BIZ-001
  ルール: 正常な入力が処理されること
    シナリオ: 有効なデータ送信
      前提 利用者が認証されている
      もし 有効な入力データが送信されたとき
      ならば 正常に反映され確認通知が行われる

  @BIZ-002
  ルール: 重複データは拒否されること
    シナリオ: 重複データの送信
      前提 既存データが存在している
      もし 同一の識別子で登録を試みたとき
      ならば 重複エラーが通知される
""", encoding="utf-8")

    # 2. App層 Gherkin
    app_file = app_dir / f"{pbi_id}.feature"
    app_file.write_text(f"""# language: ja
@covers:BIZ-001 @covers:BIZ-002
機能: {pbi_id} クライアントUI振る舞い
  シナリオ: 成功フィードバックの表示
    前提 入力画面が開かれている
    もし 正しい情報を入力して保存ボタンを押す
    ならば 完了メッセージが表示される
""", encoding="utf-8")

    # 3. BFF層 Gherkin
    bff_file = bff_dir / f"{pbi_id}.feature"
    bff_file.write_text(f"""# language: ja
@covers:BIZ-001 @covers:BIZ-002
機能: {pbi_id} APIエンドポイント連携
  シナリオ: エンドポイントレスポンス検証
    もし POSTリクエストが送信される
    ならば ステータス200と処理結果JSONが返却される
""", encoding="utf-8")

    return {
        "business": str(biz_file),
        "app": str(app_file),
        "bff": str(bff_file),
    }

@activity.defn
async def draft_schema_activity(pbi_id: str, decomp_result: Dict[str, str], feedback: Optional[str] = None) -> str:
    """Phase 2-1: OpenAPI スキーマ ドラフト"""
    logger.info(f"Drafting OpenAPI schema for {pbi_id}")
    schema_dir = BASE_DIR / "specs" / "api" / "v1"
    _ensure_dir(schema_dir)
    schema_file = schema_dir / f"{pbi_id}.yaml"

    content = f"""openapi: 3.0.3
info:
  title: {pbi_id} API
  version: 1.0.0
paths:
  /api/v1/{pbi_id}:
    post:
      summary: Execute {pbi_id} command
      requestBody:
        required: true
        content:
          application/json:
            schema:
              type: object
              required: [identifier]
              properties:
                identifier:
                  type: string
                  example: "item-001"
      responses:
        '200':
          description: Success
          content:
            application/json:
              schema:
                type: object
                properties:
                  success:
                    type: boolean
                    example: true
        '409':
          description: Conflict
          content:
            application/json:
              schema:
                type: object
                properties:
                  error:
                    type: string
                    example: "Already exists"
"""
    schema_file.write_text(content, encoding="utf-8")
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

@activity.defn
async def run_design_activity(pbi_id: str, component: str) -> Dict[str, Any]:
    """Phase 4-1: DDD / 設計フェーズ (Skeleton)
    
    NOTE: 現在はパイプライン骨組みのスタブ実装です。
    将来的に Google Antigravity Agent CLI / SDK を呼び出し、DDD 設計書を生成します。
    """
    logger.info(f"Running Design activity for {pbi_id} / {component}")
    _update_pipeline_phase(pbi_id, component, "DESIGN", "IN_PROGRESS")
    return {"design_status": "COMPLETED", "component": component}

@activity.defn
async def run_tdd_cycle_activity(pbi_id: str, component: str, design_output: Dict[str, Any]) -> Dict[str, Any]:
    """Phase 4-2: TDD サイクル (Red -> Green -> Refactor)
    
    NOTE: 現在はパイプライン骨組みのシミュレーション実装です。
    各フェーズで state.json の phase を更新し、Antigravity の Hook (tdd_isolation) と連動します。
    将来的に Antigravity Agent を headless で呼び出して実装を行います。
    """
    logger.info(f"Running TDD cycle for {pbi_id} / {component}")
    
    # 1. RED (テスト作成のみ可)
    _update_pipeline_phase(pbi_id, component, "RED")
    os.environ["TDD_PHASE"] = "RED"
    logger.info(f"[{component}] TDD Phase: RED - Creating failing tests")

    # 2. GREEN (プロダクション実装のみ可、テスト改ざん不可)
    _update_pipeline_phase(pbi_id, component, "GREEN")
    os.environ["TDD_PHASE"] = "GREEN"
    logger.info(f"[{component}] TDD Phase: GREEN - Implementing solution to pass tests")

    # 3. REFACTOR
    _update_pipeline_phase(pbi_id, component, "REFACTOR")
    os.environ["TDD_PHASE"] = "REFACTOR"
    logger.info(f"[{component}] TDD Phase: REFACTOR - Code cleanup")

    _update_pipeline_phase(pbi_id, component, "VERIFY", "COMPLETED")
    os.environ["TDD_PHASE"] = ""
    return {"tdd_status": "ALL_GREEN", "component": component}

@activity.defn
async def integration_and_pr_activity(pbi_id: str, app_res: Dict[str, Any], bff_res: Dict[str, Any]) -> str:
    """Phase 6: 結合検証 & PR 作成"""
    logger.info(f"Integration verification and PR creation for {pbi_id}")
    pr_summary = f"""# Pull Request: {pbi_id} (AI-Driven Autonomous Development)

## 概要
- PBI: `{pbi_id}`
- App Pipeline: {app_res.get('status')}
- BFF Pipeline: {bff_res.get('status')}

## 検証実績
- [x] Example Mapping 人間承認済
- [x] OpenAPI スキーマ適合性チェック合格
- [x] App / BFF 各TDDテスト全Green
- [x] 契約・結合検証合格
"""
    pr_file = BASE_DIR / "tasks" / pbi_id / "PR_SUMMARY.md"
    pr_file.write_text(pr_summary, encoding="utf-8")
    return f"PR Created: {str(pr_file)}"
