from unittest.mock import AsyncMock, patch

import pytest

from orchestrator.interactive import InteractiveWorkflowRunner, run_single_step


@pytest.mark.asyncio
async def test_interactive_runner_happy_path(tmp_path):
    """Gate 1 / Gate 2 承認を経て最後まで完走する正常系テスト"""
    pbi_id = "PBI-INT-001"
    pbi_content = "ユーザー情報取得機能"

    gate1_called = False
    gate2_called = False

    async def mock_gate_1(path, content):
        nonlocal gate1_called
        gate1_called = True
        return True, None

    async def mock_gate_2(path, content):
        nonlocal gate2_called
        gate2_called = True
        return True, None

    async def mock_gen_example_map(pbi_id, content, fb):
        p = tmp_path / f"{pbi_id}.md"
        p.write_text("# Example Map", encoding="utf-8")
        return str(p)

    async def mock_decomp(pbi_id, path):
        return {"components": ["app", "bff"]}

    async def mock_draft_schema(pbi_id, decomp, fb):
        p = tmp_path / f"{pbi_id}.yaml"
        p.write_text("openapi: 3.1.0", encoding="utf-8")
        return str(p)

    async def mock_init_tasks(pbi_id, schema_path):
        return {}

    async def mock_design(pbi_id, comp, inst):
        return {"design_status": "COMPLETED"}

    async def mock_tdd(pbi_id, comp, des, directive):
        return {"status": "SUCCESS", "component": comp}

    async def mock_integ(pbi_id, app_res, bff_res):
        return {"status": "COMPLETED", "pr_url": "https://github.com/mock/pr/999"}

    with (
        patch("orchestrator.interactive.generate_example_map_activity", side_effect=mock_gen_example_map),
        patch("orchestrator.interactive.decompose_specs_activity", side_effect=mock_decomp),
        patch("orchestrator.interactive.draft_schema_activity", side_effect=mock_draft_schema),
        patch("orchestrator.interactive.initialize_tasks_activity", side_effect=mock_init_tasks),
        patch("orchestrator.interactive.run_design_activity", side_effect=mock_design),
        patch("orchestrator.interactive.run_tdd_cycle_activity", side_effect=mock_tdd),
        patch("orchestrator.interactive.integration_and_pr_activity", side_effect=mock_integ),
    ):
        runner = InteractiveWorkflowRunner(
            pbi_id=pbi_id,
            pbi_content=pbi_content,
            on_gate_1=mock_gate_1,
            on_gate_2=mock_gate_2,
        )
        res = await runner.run()

        assert res["status"] == "COMPLETED"
        assert res["pr_url"] == "https://github.com/mock/pr/999"
        assert gate1_called is True
        assert gate2_called is True
        assert runner.stage == "COMPLETED"


@pytest.mark.asyncio
async def test_interactive_runner_gate1_retry_loop(tmp_path):
    """Gate 1 で1回差し戻し（修正指示）が発生し、2回目で承認されるケース"""
    pbi_id = "PBI-INT-002"
    pbi_content = "注文キャンセル機能"

    gate1_attempts = 0

    async def mock_gate_1(path, content):
        nonlocal gate1_attempts
        gate1_attempts += 1
        if gate1_attempts == 1:
            return False, "ルール BIZ-002 にキャンセル期限の具体例を追加してください"
        return True, None

    async def mock_gate_2(path, content):
        return True, None

    gen_map_feedbacks = []

    async def mock_gen_example_map(pbi_id, content, fb):
        gen_map_feedbacks.append(fb)
        p = tmp_path / f"{pbi_id}.md"
        p.write_text("# Example Map", encoding="utf-8")
        return str(p)

    with (
        patch("orchestrator.interactive.generate_example_map_activity", side_effect=mock_gen_example_map),
        patch("orchestrator.interactive.decompose_specs_activity", new_callable=AsyncMock) as m_decomp,
        patch("orchestrator.interactive.draft_schema_activity", new_callable=AsyncMock) as m_schema,
        patch("orchestrator.interactive.initialize_tasks_activity", new_callable=AsyncMock),
        patch("orchestrator.interactive.run_design_activity", new_callable=AsyncMock),
        patch("orchestrator.interactive.run_tdd_cycle_activity", new_callable=AsyncMock) as m_tdd,
        patch("orchestrator.interactive.integration_and_pr_activity", new_callable=AsyncMock) as m_integ,
    ):
        m_decomp.return_value = {}
        m_schema.return_value = "dummy.yaml"
        m_tdd.return_value = {"status": "SUCCESS"}
        m_integ.return_value = {"status": "COMPLETED", "pr_url": "https://github.com/mock/pr/100"}

        runner = InteractiveWorkflowRunner(
            pbi_id=pbi_id,
            pbi_content=pbi_content,
            on_gate_1=mock_gate_1,
            on_gate_2=mock_gate_2,
        )
        res = await runner.run()

        assert res["status"] == "COMPLETED"
        assert gate1_attempts == 2
        assert gen_map_feedbacks == [None, "ルール BIZ-002 にキャンセル期限の具体例を追加してください"]


@pytest.mark.asyncio
async def test_interactive_runner_lightweight_hil_resync(tmp_path):
    """Analyzer からのスキーマ変更要求を軽量 HIL で承認し、RESYNC 後に完了するケース"""
    pbi_id = "PBI-INT-003"
    pbi_content = "カート決済機能"

    hil_called = False

    async def mock_gate_1(path, content):
        return True, None

    async def mock_gate_2(path, content):
        return True, None

    async def mock_hil(directive):
        nonlocal hil_called
        hil_called = True
        assert directive["directive"] == "SCHEMA_CHANGE"
        return True, "Approved schema change"

    tdd_call_count = 0
    received_directives = []

    async def mock_tdd(pbi_id, comp, des, directive):
        nonlocal tdd_call_count
        tdd_call_count += 1
        received_directives.append(directive)
        if tdd_call_count == 1:
            # 最初の app 実行で SCHEMA_CHANGE_REQUESTED を発生させる
            return {
                "status": "SCHEMA_CHANGE_REQUESTED",
                "component": comp,
                "directive": {
                    "directive": "SCHEMA_CHANGE",
                    "instruction": "Add payment_status field",
                    "reason": "Missing field in response",
                },
            }
        return {"status": "SUCCESS", "component": comp}

    with (
        patch("orchestrator.interactive.generate_example_map_activity", new_callable=AsyncMock) as m_map,
        patch("orchestrator.interactive.decompose_specs_activity", new_callable=AsyncMock) as m_decomp,
        patch("orchestrator.interactive.draft_schema_activity", new_callable=AsyncMock) as m_schema,
        patch("orchestrator.interactive.initialize_tasks_activity", new_callable=AsyncMock),
        patch("orchestrator.interactive.run_design_activity", new_callable=AsyncMock),
        patch("orchestrator.interactive.run_tdd_cycle_activity", side_effect=mock_tdd),
        patch("orchestrator.interactive.integration_and_pr_activity", new_callable=AsyncMock) as m_integ,
    ):
        m_map.return_value = "dummy.md"
        m_decomp.return_value = {}
        m_schema.return_value = "dummy.yaml"
        m_integ.return_value = {"status": "COMPLETED", "pr_url": "https://github.com/mock/pr/101"}

        runner = InteractiveWorkflowRunner(
            pbi_id=pbi_id,
            pbi_content=pbi_content,
            on_gate_1=mock_gate_1,
            on_gate_2=mock_gate_2,
            on_lightweight_hil=mock_hil,
        )
        res = await runner.run()

        assert res["status"] == "COMPLETED"
        assert hil_called is True
        # attempt 0 で app と bff が起動し、RESYNC 後に再度起動
        assert tdd_call_count >= 3
        # RESYNC directive が渡されていること
        resync_dirs = [d for d in received_directives if d and d.get("directive") == "RESYNC"]
        assert len(resync_dirs) >= 1


@pytest.mark.asyncio
async def test_interactive_runner_lightweight_hil_rejection():
    """軽量 HIL でスキーマ変更が却下された場合、安全に SCHEMA_CHANGE_REJECTED で終了するケース"""
    pbi_id = "PBI-INT-004"
    pbi_content = "在庫引当機能"

    async def mock_gate_1(path, content):
        return True, None

    async def mock_gate_2(path, content):
        return True, None

    async def mock_hil(directive):
        return False, "Do not alter schema at this time"

    async def mock_tdd(pbi_id, comp, des, directive):
        return {
            "status": "SCHEMA_CHANGE_REQUESTED",
            "component": comp,
            "directive": {"directive": "SCHEMA_CHANGE", "instruction": "Modify fields"},
        }

    with (
        patch("orchestrator.interactive.generate_example_map_activity", new_callable=AsyncMock) as m_map,
        patch("orchestrator.interactive.decompose_specs_activity", new_callable=AsyncMock),
        patch("orchestrator.interactive.draft_schema_activity", new_callable=AsyncMock) as m_schema,
        patch("orchestrator.interactive.initialize_tasks_activity", new_callable=AsyncMock),
        patch("orchestrator.interactive.run_design_activity", new_callable=AsyncMock),
        patch("orchestrator.interactive.run_tdd_cycle_activity", side_effect=mock_tdd),
    ):
        m_map.return_value = "dummy.md"
        m_schema.return_value = "dummy.yaml"

        runner = InteractiveWorkflowRunner(
            pbi_id=pbi_id,
            pbi_content=pbi_content,
            on_gate_1=mock_gate_1,
            on_gate_2=mock_gate_2,
            on_lightweight_hil=mock_hil,
        )
        res = await runner.run()

        assert res["status"] == "FAILED"
        assert res["stage"] == "SCHEMA_CHANGE_REJECTED"
        assert "Schema change rejected" in res["reason"]


@pytest.mark.asyncio
async def test_run_single_step():
    """run_single_step ヘルパーが各個別ステップを正しく実行できること"""
    pbi_id = "PBI-INT-005"
    with patch("orchestrator.interactive.generate_example_map_activity", new_callable=AsyncMock) as m_map:
        m_map.return_value = "dummy.md"
        res = await run_single_step("example_map", pbi_id, "content")
        assert res["step"] == "example_map"
        assert res["path"] == "dummy.md"
