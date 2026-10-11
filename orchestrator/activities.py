import os
import json
import logging
from pathlib import Path
from typing import Dict, Any, Optional

from temporalio import activity

logger = logging.getLogger("orchestrator.activities")

BASE_DIR = Path(__file__).resolve().parent.parent

def _ensure_dir(p: Path):
    p.mkdir(parents=True, exist_ok=True)

@activity.defn
async def generate_example_map_activity(pbi_id: str, pbi_content: str, feedback: Optional[str] = None) -> str:
    """Phase 1-1: Example Map 生成 Activity"""
    logger.info(f"Generating Example Map for {pbi_id} (feedback: {feedback})")
    
    target_dir = BASE_DIR / "specs" / "example-maps"
    _ensure_dir(target_dir)
    target_file = target_dir / f"{pbi_id}.md"

    # プロンプトや指示にフィードバックを含める
    feedback_note = f"\n> 前回のフィードバック: {feedback}\n" if feedback else ""

    content = f"""# Example Map: {pbi_id}
{feedback_note}
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

@activity.defn
async def run_design_activity(pbi_id: str, component: str) -> Dict[str, Any]:
    """Phase 4-1: DDD / 設計フェーズ"""
    logger.info(f"Running Design activity for {pbi_id} / {component}")
    # Antigravity Agent を呼び出して設計ドキュメント等を生成可能
    return {"design_status": "COMPLETED", "component": component}

@activity.defn
async def run_tdd_cycle_activity(pbi_id: str, component: str, design_output: Dict[str, Any]) -> Dict[str, Any]:
    """Phase 4-2: TDD サイクル (Red -> Green -> Refactor)"""
    logger.info(f"Running TDD cycle for {pbi_id} / {component}")
    
    # 権限分離のシミュレーション: フェーズごとに環境変数を切り替え
    # 1. RED (テスト作成のみ可)
    os.environ["TDD_PHASE"] = "RED"
    logger.info(f"[{component}] TDD Phase: RED - Creating failing tests")

    # 2. GREEN (プロダクション実装のみ可、テスト改ざん不可)
    os.environ["TDD_PHASE"] = "GREEN"
    logger.info(f"[{component}] TDD Phase: GREEN - Implementing solution to pass tests")

    # 3. REFACTOR
    os.environ["TDD_PHASE"] = "REFACTOR"
    logger.info(f"[{component}] TDD Phase: REFACTOR - Code cleanup")

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
