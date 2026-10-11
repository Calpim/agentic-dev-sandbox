from datetime import timedelta
import asyncio
from typing import Optional, Dict, Any, List
from dataclasses import dataclass

from temporalio import workflow
from temporalio.common import RetryPolicy

# Activity 定義のインポート準備（名前ベースで呼ぶためインターフェースまたは文字列参照）

@dataclass
class WorkflowState:
    pbi_id: str
    example_map_approved: bool = False
    example_map_feedback: Optional[str] = None
    schema_approved: bool = False
    schema_feedback: Optional[str] = None
    stage: str = "INIT"
    pr_url: Optional[str] = None
    pr_summary_path: Optional[str] = None

@workflow.defn
class PBIDevelopmentWorkflow:
    """
    PBI 単位の自律開発メインワークフロー (v3.0)
    Temporal による状態永続化と Human-in-the-Loop 制御
    """
    def __init__(self):
        self.state = WorkflowState(pbi_id="")

    @workflow.signal
    def approve_example_map(self):
        """人間ゲート 1: Example Map 承認"""
        self.state.example_map_approved = True
        self.state.example_map_feedback = None

    @workflow.signal
    def reject_example_map(self, feedback: str):
        """人間ゲート 1: Example Map 修正依頼"""
        self.state.example_map_approved = False
        self.state.example_map_feedback = feedback

    @workflow.signal
    def approve_schema(self):
        """人間ゲート 2: OpenAPI スキーマ承認"""
        self.state.schema_approved = True
        self.state.schema_feedback = None

    @workflow.signal
    def reject_schema(self, feedback: str):
        """人間ゲート 2: スキーマ修正依頼"""
        self.state.schema_approved = False
        self.state.schema_feedback = feedback

    @workflow.query
    def get_status(self) -> Dict[str, Any]:
        """現在の状態を照会"""
        return {
            "pbi_id": self.state.pbi_id,
            "stage": self.state.stage,
            "example_map_approved": self.state.example_map_approved,
            "schema_approved": self.state.schema_approved,
            "pr_url": self.state.pr_url,
            "pr_summary_path": self.state.pr_summary_path,
            "feedback": {
                "example_map": self.state.example_map_feedback,
                "schema": self.state.schema_feedback,
            }
        }

    @workflow.run
    async def run(self, pbi_id: str, pbi_content: str) -> Dict[str, Any]:
        self.state.pbi_id = pbi_id
        self.state.stage = "PHASE_1_EXAMPLE_MAPPING"

        # ---------------------------------------------------------
        # Phase 1-1: Example Map 生成
        # ---------------------------------------------------------
        retry_policy = RetryPolicy(maximum_attempts=3)
        
        while not self.state.example_map_approved:
            feedback_to_apply = self.state.example_map_feedback
            self.state.example_map_feedback = None

            example_map_path = await workflow.execute_activity(
                "generate_example_map_activity",
                args=[pbi_id, pbi_content, feedback_to_apply],
                start_to_close_timeout=timedelta(minutes=5),
                retry_policy=retry_policy,
            )

            # ★ 人間ゲート 1: ユーザーの承認または修正指示を待機 (Durable wait)
            self.state.stage = "WAITING_HUMAN_GATE_1_EXAMPLE_MAP"
            await workflow.wait_condition(
                lambda: self.state.example_map_approved or (self.state.example_map_feedback is not None)
            )

            if self.state.example_map_feedback:
                # 差し戻し時の再生成ループ
                self.state.stage = "RETRYING_EXAMPLE_MAP"
                continue

        # ---------------------------------------------------------
        # Phase 1-2 & 1-3: Gherkin 生成 & App/BFF 分解
        # ---------------------------------------------------------
        self.state.stage = "PHASE_1_GHERKIN_DECOMPOSITION"
        decomp_result = await workflow.execute_activity(
            "decompose_specs_activity",
            args=[pbi_id, example_map_path],
            start_to_close_timeout=timedelta(minutes=5),
            retry_policy=retry_policy,
        )

        # ---------------------------------------------------------
        # Phase 2: Schema First (OpenAPI)
        # ---------------------------------------------------------
        self.state.stage = "PHASE_2_SCHEMA_DRAFT"
        while not self.state.schema_approved:
            feedback_to_apply = self.state.schema_feedback
            self.state.schema_feedback = None

            schema_path = await workflow.execute_activity(
                "draft_schema_activity",
                args=[pbi_id, decomp_result, feedback_to_apply],
                start_to_close_timeout=timedelta(minutes=5),
                retry_policy=retry_policy,
            )

            # ★ 人間ゲート 2: スキーマ承認待機
            self.state.stage = "WAITING_HUMAN_GATE_2_SCHEMA"
            await workflow.wait_condition(
                lambda: self.state.schema_approved or (self.state.schema_feedback is not None)
            )

            if self.state.schema_feedback:
                self.state.stage = "RETRYING_SCHEMA"
                continue

        # Phase 2-2: モック生成 & 状態初期化
        await workflow.execute_activity(
            "initialize_tasks_activity",
            args=[pbi_id, schema_path],
            start_to_close_timeout=timedelta(minutes=3),
        )

        # ---------------------------------------------------------
        # Phase 3 & 4: App / BFF 並行パイプライン起動
        # ---------------------------------------------------------
        self.state.stage = "PHASE_3_4_PARALLEL_EXECUTION"
        app_task = workflow.execute_child_workflow(
            ComponentPipelineWorkflow,
            args=[pbi_id, "app"],
            id=f"{pbi_id}-pipeline-app",
        )
        bff_task = workflow.execute_child_workflow(
            ComponentPipelineWorkflow,
            args=[pbi_id, "bff"],
            id=f"{pbi_id}-pipeline-bff",
        )

        # 両パイプラインの完了を待機
        app_res, bff_res = await asyncio.gather(app_task, bff_task)

        # ---------------------------------------------------------
        # Phase 6: 結合検証 & PR 作成
        # ---------------------------------------------------------
        self.state.stage = "PHASE_6_INTEGRATION_AND_PR"
        pr_result = await workflow.execute_activity(
            "integration_and_pr_activity",
            args=[pbi_id, app_res, bff_res],
            start_to_close_timeout=timedelta(minutes=10),
        )

        self.state.pr_url = pr_result.get("pr_url")
        self.state.pr_summary_path = pr_result.get("pr_summary_path")
        self.state.stage = "COMPLETED"
        return pr_result


@workflow.defn
class ComponentPipelineWorkflow:
    """
    担当別 (App または BFF) の自律設計・TDDサイクル
    """
    @workflow.run
    async def run(self, pbi_id: str, component: str) -> Dict[str, Any]:
        retry_policy = RetryPolicy(maximum_attempts=2)

        # Phase 4-1: DDD / 設計
        design_output = await workflow.execute_activity(
            "run_design_activity",
            args=[pbi_id, component],
            start_to_close_timeout=timedelta(minutes=10),
            retry_policy=retry_policy,
        )

        # Phase 4-2 & 5: TDD 実装ループ (Red -> Green -> Refactor -> Verify)
        tdd_output = await workflow.execute_activity(
            "run_tdd_cycle_activity",
            args=[pbi_id, component, design_output],
            start_to_close_timeout=timedelta(minutes=20),
        )

        return {
            "pbi_id": pbi_id,
            "component": component,
            "status": "SUCCESS",
            "details": tdd_output,
        }
