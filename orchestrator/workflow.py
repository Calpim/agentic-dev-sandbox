import asyncio
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any, Dict, List, Optional

from temporalio import workflow
from temporalio.common import RetryPolicy

from orchestrator.guards.validators import validate_pbi_id

# Activity 定義のインポート準備（名前ベースで呼ぶためインターフェースまたは文字列参照）


@dataclass
class WorkflowState:
    pbi_id: str
    example_map_approved: bool = False
    example_map_feedback: Optional[str] = None
    schema_approved: bool = False
    schema_feedback: Optional[str] = None
    schema_change_approved: bool = False
    schema_change_feedback: Optional[str] = None
    stage: str = "INIT"
    pr_url: Optional[str] = None
    pr_summary_path: Optional[str] = None
    ignored_signals: List[str] = field(default_factory=list)


def _safe_log_warning(msg: str) -> None:
    try:
        workflow.logger.warning(msg)
    except Exception:
        import logging

        logging.getLogger("orchestrator.workflow").warning(msg)


@workflow.defn
class PBIDevelopmentWorkflow:
    """
    PBI 単位の自律開発メインワークフロー (v3.0)
    Temporal による状態永続化と Human-in-the-Loop 制御 (P2-5 スキーマ変更自律同期対応)
    """

    def __init__(self):
        self.state = WorkflowState(pbi_id="")

    @workflow.signal
    def approve_example_map(self):
        """人間ゲート 1: Example Map 承認 (Issue #18)"""
        if self.state.stage != "WAITING_HUMAN_GATE_1_EXAMPLE_MAP":
            msg = f"Ignored approve_example_map signal at unexpected stage: {self.state.stage}"
            _safe_log_warning(msg)
            self.state.ignored_signals.append(msg)
            return
        self.state.example_map_approved = True
        self.state.example_map_feedback = None

    @workflow.signal
    def reject_example_map(self, feedback: str):
        """人間ゲート 1: Example Map 修正依頼 (Issue #18)"""
        if self.state.stage != "WAITING_HUMAN_GATE_1_EXAMPLE_MAP":
            msg = f"Ignored reject_example_map signal at unexpected stage: {self.state.stage}"
            _safe_log_warning(msg)
            self.state.ignored_signals.append(msg)
            return
        self.state.example_map_approved = False
        self.state.example_map_feedback = feedback

    @workflow.signal
    def approve_schema(self):
        """人間ゲート 2: OpenAPI スキーマ承認 (Issue #18)"""
        if self.state.stage != "WAITING_HUMAN_GATE_2_SCHEMA":
            msg = f"Ignored approve_schema signal at unexpected stage: {self.state.stage}"
            _safe_log_warning(msg)
            self.state.ignored_signals.append(msg)
            return
        self.state.schema_approved = True
        self.state.schema_feedback = None

    @workflow.signal
    def reject_schema(self, feedback: str):
        """人間ゲート 2: スキーマ修正依頼 (Issue #18)"""
        if self.state.stage != "WAITING_HUMAN_GATE_2_SCHEMA":
            msg = f"Ignored reject_schema signal at unexpected stage: {self.state.stage}"
            _safe_log_warning(msg)
            self.state.ignored_signals.append(msg)
            return
        self.state.schema_approved = False
        self.state.schema_feedback = feedback

    @workflow.signal
    def approve_schema_change(self, feedback: Optional[str] = None):
        """軽量 HIL (P2-5): スキーマ変更差分の承認 (Issue #18)"""
        if self.state.stage != "WAITING_LIGHTWEIGHT_HIL_SCHEMA_CHANGE":
            msg = f"Ignored approve_schema_change signal at unexpected stage: {self.state.stage}"
            _safe_log_warning(msg)
            self.state.ignored_signals.append(msg)
            return
        self.state.schema_change_approved = True
        self.state.schema_change_feedback = feedback

    @workflow.signal
    def reject_schema_change(self, feedback: str):
        """軽量 HIL (P2-5): スキーマ変更差分の却下 (Issue #18)"""
        if self.state.stage != "WAITING_LIGHTWEIGHT_HIL_SCHEMA_CHANGE":
            msg = f"Ignored reject_schema_change signal at unexpected stage: {self.state.stage}"
            _safe_log_warning(msg)
            self.state.ignored_signals.append(msg)
            return
        self.state.schema_change_approved = False
        self.state.schema_change_feedback = feedback

    @workflow.query
    def get_status(self) -> Dict[str, Any]:
        """現在の状態を照会"""
        return {
            "pbi_id": self.state.pbi_id,
            "stage": self.state.stage,
            "example_map_approved": self.state.example_map_approved,
            "schema_approved": self.state.schema_approved,
            "schema_change_approved": self.state.schema_change_approved,
            "pr_url": self.state.pr_url,
            "pr_summary_path": self.state.pr_summary_path,
            "ignored_signals": self.state.ignored_signals,
            "feedback": {
                "example_map": self.state.example_map_feedback,
                "schema": self.state.schema_feedback,
                "schema_change": self.state.schema_change_feedback,
            },
        }

    @workflow.run
    async def run(self, pbi_id: str, pbi_content: str) -> Dict[str, Any]:
        try:
            pbi_id = validate_pbi_id(pbi_id)
        except ValueError as e:
            from temporalio.exceptions import ApplicationError

            raise ApplicationError(str(e), non_retryable=True)

        self.state.pbi_id = pbi_id
        self.state.stage = "PHASE_1_EXAMPLE_MAPPING"

        # ---------------------------------------------------------
        # Phase 1-1: Example Map 生成
        # ---------------------------------------------------------
        retry_policy = RetryPolicy(maximum_attempts=3)
        example_map_path = f"specs/example-maps/{pbi_id}.md"
        decomp_result: Dict[str, Any] = {}
        schema_path = f"specs/api/v1/{pbi_id}.yaml"

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
                # 差し戻し時の再生成ループ: 承認フラグを安全にリセット
                self.state.stage = "RETRYING_EXAMPLE_MAP"
                self.state.example_map_approved = False
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
                self.state.schema_approved = False
                continue

        # Phase 2-2: モック生成 & 状態初期化
        await workflow.execute_activity(
            "initialize_tasks_activity",
            args=[pbi_id, schema_path],
            start_to_close_timeout=timedelta(minutes=3),
        )

        # ---------------------------------------------------------
        # Phase 3 & 4: App / BFF 並行パイプライン起動 (P2-5 RESYNC ループ対応)
        # ---------------------------------------------------------
        max_resync_attempts = 2
        app_directive: Optional[Dict[str, Any]] = None
        bff_directive: Optional[Dict[str, Any]] = None

        app_res: Dict[str, Any] = {}
        bff_res: Dict[str, Any] = {}

        for attempt in range(max_resync_attempts + 1):
            self.state.stage = "PHASE_3_4_PARALLEL_EXECUTION"
            app_task = workflow.execute_child_workflow(
                ComponentPipelineWorkflow.run,
                args=[pbi_id, "app", app_directive],
                id=f"{pbi_id}-pipeline-app-attempt-{attempt}",
            )
            bff_task = workflow.execute_child_workflow(
                ComponentPipelineWorkflow.run,
                args=[pbi_id, "bff", bff_directive],
                id=f"{pbi_id}-pipeline-bff-attempt-{attempt}",
            )

            # 両パイプラインの完了を待機
            app_res, bff_res = await asyncio.gather(app_task, bff_task)

            # スキーマ変更要求 (SCHEMA_CHANGE_REQUESTED) の検知
            app_needs_change = app_res.get("status") == "SCHEMA_CHANGE_REQUESTED"
            bff_needs_change = bff_res.get("status") == "SCHEMA_CHANGE_REQUESTED"

            if app_needs_change or bff_needs_change:
                raw_directive = app_res.get("directive") if app_needs_change else bff_res.get("directive")
                change_directive: Dict[str, Any] = raw_directive if isinstance(raw_directive, dict) else {}
                change_instruction = change_directive.get("instruction", "Schema change requested by Analyzer")

                # ★ 軽量 HIL: スキーマ変更差分の承認待機
                self.state.stage = "WAITING_LIGHTWEIGHT_HIL_SCHEMA_CHANGE"
                self.state.schema_change_approved = False
                self.state.schema_change_feedback = None

                await workflow.wait_condition(
                    lambda: self.state.schema_change_approved or (self.state.schema_change_feedback is not None)
                )

                if self.state.schema_change_approved:
                    # 承認時: スキーマ更新 & モック再初期化
                    self.state.stage = "APPLYING_SCHEMA_RESYNC"
                    feedback_str = self.state.schema_change_feedback or change_instruction
                    schema_path = await workflow.execute_activity(
                        "draft_schema_activity",
                        args=[pbi_id, decomp_result, feedback_str],
                        start_to_close_timeout=timedelta(minutes=5),
                        retry_policy=retry_policy,
                    )
                    await workflow.execute_activity(
                        "initialize_tasks_activity",
                        args=[pbi_id, schema_path],
                        start_to_close_timeout=timedelta(minutes=3),
                    )

                    # 両パイプラインへ配信する RESYNC Directive
                    resync_dir = {
                        "directive": "RESYNC",
                        "instruction": f"OpenAPI schema has been updated and approved (attempt {attempt + 1}). Resync models and resume.",
                        "reason": change_directive.get("reason", "Schema modified via lightweight HIL"),
                    }
                    app_directive = resync_dir
                    bff_directive = resync_dir
                    continue
                else:
                    # 拒否時: パイプライン中断
                    self.state.stage = "SCHEMA_CHANGE_REJECTED"
                    return {
                        "pbi_id": pbi_id,
                        "status": "FAILED",
                        "reason": f"Schema change rejected: {self.state.schema_change_feedback}",
                        "app": app_res,
                        "bff": bff_res,
                    }

            # スキーマ変更要求がなければループ終了
            break

        # パイプライン成否チェック (いずれかが失敗またはエスカレーション時は中断)
        if app_res.get("status") != "SUCCESS" or bff_res.get("status") != "SUCCESS":
            self.state.stage = "PIPELINE_FAILED"
            return {
                "pbi_id": pbi_id,
                "status": "FAILED",
                "app": app_res,
                "bff": bff_res,
            }

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
        if pr_result.get("status") == "COMPLETED":
            self.state.stage = "COMPLETED"
        else:
            self.state.stage = "INTEGRATION_FAILED"
        return pr_result


@workflow.defn
class ComponentPipelineWorkflow:
    """
    担当別 (App または BFF) の自律設計・TDDサイクル (v3.0 + Analyzer自律自己修復 + P2-5 RESYNC)
    """

    @workflow.run
    async def run(
        self, pbi_id: str, component: str, initial_directive: Optional[Dict[str, Any]] = None
    ) -> Dict[str, Any]:
        retry_policy = RetryPolicy(maximum_attempts=2)

        design_feedback = initial_directive.get("instruction") if initial_directive else None

        # Phase 4-1: DDD / 設計
        design_output = await workflow.execute_activity(
            "run_design_activity",
            args=[pbi_id, component, design_feedback],
            start_to_close_timeout=timedelta(minutes=10),
            retry_policy=retry_policy,
        )

        current_directive: Optional[Dict[str, Any]] = initial_directive
        tdd_output: Dict[str, Any] = {}
        max_analyzer_cycles = 2

        # Phase 4-2 & 5: TDD 実装・検証 & Analyzer 自律自己修復ループ
        for _ in range(max_analyzer_cycles + 1):
            tdd_output = await workflow.execute_activity(
                "run_tdd_cycle_activity",
                args=[pbi_id, component, design_output, current_directive],
                start_to_close_timeout=timedelta(minutes=20),
            )

            if tdd_output.get("tdd_status") == "ALL_GREEN":
                return {
                    "pbi_id": pbi_id,
                    "component": component,
                    "status": "SUCCESS",
                    "details": tdd_output,
                }

            # TDD 失敗時: Phase 5-3 Analyzer による根本原因分析 & Directive 策定
            analyzer_res = await workflow.execute_activity(
                "run_analyzer_activity",
                args=[pbi_id, component, tdd_output],
                start_to_close_timeout=timedelta(minutes=5),
            )

            current_directive = analyzer_res.get("directive", {})
            directive_type = current_directive.get("directive")

            if directive_type == "ESCALATE_HUMAN":
                return {
                    "pbi_id": pbi_id,
                    "component": component,
                    "status": "ESCALATED_HUMAN",
                    "details": tdd_output,
                    "directive": current_directive,
                }
            elif directive_type == "SCHEMA_CHANGE":
                # P2-5: 親ワークフローにスキーマ変更要求を返却し、軽量 HIL へ
                return {
                    "pbi_id": pbi_id,
                    "component": component,
                    "status": "SCHEMA_CHANGE_REQUESTED",
                    "details": tdd_output,
                    "directive": current_directive,
                }
            elif directive_type == "RETRY_DESIGN":
                # Phase 4-1 再設計へ差し戻し
                design_output = await workflow.execute_activity(
                    "run_design_activity",
                    args=[pbi_id, component, current_directive.get("instruction")],
                    start_to_close_timeout=timedelta(minutes=10),
                    retry_policy=retry_policy,
                )
                current_directive = None
            elif directive_type in ("FIX_TEST", "RETRY_IMPL", "RESYNC"):
                # 指示内容を持って次の TDD サイクルへ (FIX_TEST/RETRY_IMPL: Analyzer指示, RESYNC: 親ワークフローからの再同期初期指示)
                continue
            else:
                break

        return {
            "pbi_id": pbi_id,
            "component": component,
            "status": "FAILED",
            "details": tdd_output,
            "directive": current_directive,
        }
