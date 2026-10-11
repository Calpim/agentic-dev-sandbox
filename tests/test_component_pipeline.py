import os
import shutil
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from orchestrator.activities import (
    BASE_DIR,
    run_design_activity,
    run_tdd_cycle_activity,
)


@pytest.fixture(autouse=True)
def cleanup_generated_artifacts():
    """テスト実行前後に生成ファイルを自動クリーンアップして冪等性を担保する"""

    def _do_clean():
        for pbi in ["PBI-501", "PBI-502", "PBI-503", "PBI-504", "PBI-505", "PBI-506"]:
            clean_id = pbi.lower().replace("-", "_")
            for comp in ("app", "bff"):
                test_file = BASE_DIR / "src" / comp / "tests" / f"test_{clean_id}.py"
                bdd_file = BASE_DIR / "src" / comp / "tests" / f"test_{clean_id}_bdd.py"
                impl_file = BASE_DIR / "src" / comp / f"{clean_id}_service.py"
                if test_file.exists():
                    test_file.unlink()
                if bdd_file.exists():
                    bdd_file.unlink()
                if impl_file.exists():
                    impl_file.unlink()

            task_dir = BASE_DIR / "tasks" / pbi
            if task_dir.exists():
                shutil.rmtree(task_dir, ignore_errors=True)

    _do_clean()
    yield
    _do_clean()


@pytest.mark.asyncio
async def test_run_design_activity_fallback():
    pbi_id = "PBI-501"
    component = "app"

    with patch.dict(os.environ, {"GEMINI_API_KEY": "", "GOOGLE_API_KEY": ""}):
        with patch("orchestrator.activities._call_gemini_for_design", new_callable=AsyncMock) as mock_gemini:
            mock_gemini.return_value = None

            result = await run_design_activity(pbi_id, component)
            assert result.get("design_status") == "COMPLETED"
            assert result.get("component") == component
            assert Path(str(result.get("design_path"))).exists()

            content = Path(str(result.get("design_path"))).read_text(encoding="utf-8")
            assert f"Architecture & DDD Design: {pbi_id}" in content
            assert "Service" in content


@pytest.mark.asyncio
async def test_run_design_activity_gemini_mock():
    pbi_id = "PBI-502"
    component = "bff"
    mock_design = "# LLM Generated Architecture Design\n## Components\n- Controller\n- Service"

    with patch("orchestrator.activities._call_gemini_for_design", new_callable=AsyncMock) as mock_gemini:
        mock_gemini.return_value = mock_design

        result = await run_design_activity(pbi_id, component)
        assert result.get("design_status") == "COMPLETED"
        content = Path(str(result.get("design_path"))).read_text(encoding="utf-8")
        assert content == mock_design


@pytest.mark.asyncio
async def test_run_tdd_cycle_activity_execution():
    pbi_id = "PBI-503"
    component = "bff"
    design_output = {"design_status": "COMPLETED"}

    result = await run_tdd_cycle_activity(pbi_id, component, design_output)

    assert result.get("tdd_status") == "ALL_GREEN"
    assert result.get("component") == component
    assert Path(str(result.get("test_file"))).exists()
    assert Path(str(result.get("impl_file"))).exists()

    rule_guard_data = result.get("rule_guard", {})
    assert rule_guard_data.get("category") == "SUCCESS"
    assert rule_guard_data.get("next_action") == "PROCEED"


@pytest.mark.asyncio
async def test_run_tdd_cycle_red_phase_violation():
    """REDフェーズでテストが意図せず成功してしまった場合、パイプラインが中断されること"""
    pbi_id = "PBI-504"
    component = "app"
    design_output = {"design_status": "COMPLETED"}

    # 最初からパスしてしまう空のテストコードを生成させるモック
    dummy_pass_test = "def test_always_pass(): assert True\n"
    with patch("orchestrator.activities._generate_test_code", return_value=dummy_pass_test):
        result = await run_tdd_cycle_activity(pbi_id, component, design_output)
        assert result.get("tdd_status") == "RED_PHASE_VIOLATION"
        assert "TDD RED phase violation" in result.get("error", "")


@pytest.mark.asyncio
async def test_run_tdd_cycle_green_failure():
    """GREENフェーズで実装がテストを満たせない場合、早期エラー返却とRule Guard分類が行われること"""
    pbi_id = "PBI-505"
    component = "bff"
    design_output = {"design_status": "COMPLETED"}

    # テストを失敗させる壊れた実装コードを生成させるモック
    broken_impl = (
        "class Pbi505Service:\n    def execute(self, payload):\n        return {'success': False, 'status_code': 500}\n"
    )
    with patch("orchestrator.activities._generate_impl_code", return_value=broken_impl):
        result = await run_tdd_cycle_activity(pbi_id, component, design_output)
        assert result.get("tdd_status") == "GREEN_FAILED"
        rule_guard_data = result.get("rule_guard", {})
        assert rule_guard_data.get("category") == "TEST_FAILURE"
