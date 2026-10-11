from unittest.mock import patch

import pytest

from orchestrator.guards.analyzer import DirectiveType
from orchestrator.workflow import ComponentPipelineWorkflow, PBIDevelopmentWorkflow


def test_directive_type_has_resync():
    """DirectiveType に RESYNC が定義されていること"""
    assert DirectiveType.RESYNC.value == "RESYNC"


@pytest.mark.asyncio
async def test_component_pipeline_handles_resync_directive():
    """ComponentPipelineWorkflow が RESYNC 指示を受け取り、TDD サイクルを完遂できること"""
    pbi_id = "PBI-RESYNC-001"
    component = "app"
    initial_directive = {
        "directive": "RESYNC",
        "instruction": "OpenAPI schema was updated. Resync models.",
        "reason": "Lightweight HIL approved",
    }

    design_called = False
    tdd_called = False

    async def mock_execute_activity(activity_name, args, **kwargs):
        nonlocal design_called, tdd_called
        if activity_name == "run_design_activity":
            design_called = True
            assert args[2] == "OpenAPI schema was updated. Resync models."
            return {"design_status": "COMPLETED"}
        elif activity_name == "run_tdd_cycle_activity":
            tdd_called = True
            assert args[3] == initial_directive
            return {"tdd_status": "ALL_GREEN"}
        return {}

    wf = ComponentPipelineWorkflow()
    with patch("temporalio.workflow.execute_activity", side_effect=mock_execute_activity):
        result = await wf.run(pbi_id, component, initial_directive)
        assert result["status"] == "SUCCESS"
        assert design_called is True
        assert tdd_called is True


@pytest.mark.asyncio
async def test_pbi_development_workflow_resync_loop_approval():
    """PBIDevelopmentWorkflow が SCHEMA_CHANGE_REQUESTED を検知し、軽量 HIL 承認後に RESYNC で再開すること"""
    pbi_id = "PBI-RESYNC-002"
    wf = PBIDevelopmentWorkflow()
    wf.state.pbi_id = pbi_id
    wf.state.example_map_approved = True
    wf.state.schema_approved = True

    child_workflow_attempts = []
    draft_schema_call_count = 0
    init_tasks_call_count = 0

    async def mock_execute_child_workflow(wf_func, args, id, **kwargs):
        child_workflow_attempts.append((args[1], args[2] if len(args) > 2 else None))
        # 1回目の実行 (attempt 0): app が SCHEMA_CHANGE_REQUESTED を返す
        if len(child_workflow_attempts) <= 2:
            if args[1] == "app":
                return {
                    "status": "SCHEMA_CHANGE_REQUESTED",
                    "directive": {
                        "directive": "SCHEMA_CHANGE",
                        "instruction": "Add status field to response",
                        "reason": "Missing schema field",
                    },
                }
            else:
                return {"status": "SUCCESS"}
        # 2回目の実行 (attempt 1): RESYNC 後、両方 SUCCESS となる
        return {"status": "SUCCESS", "tdd_status": "ALL_GREEN"}

    async def mock_execute_activity(activity_name, args, **kwargs):
        nonlocal draft_schema_call_count, init_tasks_call_count
        if activity_name == "draft_schema_activity":
            draft_schema_call_count += 1
            return "specs/api/v1/PBI-RESYNC-002.yaml"
        elif activity_name == "initialize_tasks_activity":
            init_tasks_call_count += 1
            return {}
        elif activity_name == "integration_and_pr_activity":
            return {"status": "COMPLETED", "pr_url": "https://github.com/mock/pr/123"}
        return {}

    async def mock_wait_condition(fn):
        while not fn():
            await asyncio.sleep(0.001)

    with (
        patch("temporalio.workflow.execute_child_workflow", side_effect=mock_execute_child_workflow),
        patch("temporalio.workflow.execute_activity", side_effect=mock_execute_activity),
        patch("temporalio.workflow.wait_condition", side_effect=mock_wait_condition),
    ):
        # シグナル送信をシミュレートするコルーチン
        async def run_wf():
            return await wf.run(pbi_id, "Description")

        # バックグラウンドで待機中のシグナルを送信
        async def send_signal_when_waiting():
            while wf.state.stage != "WAITING_LIGHTWEIGHT_HIL_SCHEMA_CHANGE":
                await asyncio.sleep(0.001)
            wf.approve_schema_change(feedback="Approved with status field")

        import asyncio

        wf_task = asyncio.create_task(run_wf())
        sig_task = asyncio.create_task(send_signal_when_waiting())

        result, _ = await asyncio.gather(wf_task, sig_task)

        assert result["status"] == "COMPLETED"
        assert wf.state.stage == "COMPLETED"
        assert draft_schema_call_count == 1  # RESYNC でスキーマ再ドラフト
        assert init_tasks_call_count == 2  # 初回初期化 + RESYNC 時のモック再初期化
        assert len(child_workflow_attempts) == 4  # app/bff attempt 0 + app/bff attempt 1
        # attempt 1 に渡されたディレクティブが RESYNC であること
        attempt1_app_dir = child_workflow_attempts[2][1]
        attempt1_bff_dir = child_workflow_attempts[3][1]
        assert isinstance(attempt1_app_dir, dict)
        assert isinstance(attempt1_bff_dir, dict)
        assert attempt1_app_dir["directive"] == "RESYNC"
        assert attempt1_bff_dir["directive"] == "RESYNC"


@pytest.mark.asyncio
async def test_pbi_development_workflow_resync_loop_rejection():
    """軽量 HIL でスキーマ変更が却下された場合、ワークフローが安全に中断すること"""
    pbi_id = "PBI-RESYNC-003"
    wf = PBIDevelopmentWorkflow()
    wf.state.pbi_id = pbi_id
    wf.state.example_map_approved = True
    wf.state.schema_approved = True

    async def mock_execute_child_workflow(wf_func, args, id, **kwargs):
        if args[1] == "app":
            return {
                "status": "SCHEMA_CHANGE_REQUESTED",
                "directive": {"directive": "SCHEMA_CHANGE", "instruction": "Invalid change"},
            }
        return {"status": "SUCCESS"}

    async def mock_execute_activity(activity_name, args, **kwargs):
        return {}

    async def mock_wait_condition(fn):
        while not fn():
            await asyncio.sleep(0.001)

    with (
        patch("temporalio.workflow.execute_child_workflow", side_effect=mock_execute_child_workflow),
        patch("temporalio.workflow.execute_activity", side_effect=mock_execute_activity),
        patch("temporalio.workflow.wait_condition", side_effect=mock_wait_condition),
    ):

        async def run_wf():
            return await wf.run(pbi_id, "Description")

        async def send_rejection():
            while wf.state.stage != "WAITING_LIGHTWEIGHT_HIL_SCHEMA_CHANGE":
                await asyncio.sleep(0.001)
            wf.reject_schema_change(feedback="Rejected by user: do not modify schema")

        import asyncio

        wf_task = asyncio.create_task(run_wf())
        sig_task = asyncio.create_task(send_rejection())

        result, _ = await asyncio.gather(wf_task, sig_task)

        assert result["status"] == "FAILED"
        assert "Schema change rejected" in result["reason"]
        assert wf.state.stage == "SCHEMA_CHANGE_REJECTED"
