from unittest.mock import AsyncMock, patch

import pytest

from orchestrator.activities import BASE_DIR, run_tdd_cycle_activity


@pytest.fixture(autouse=True)
def cleanup_generated_artifacts():
    def _clean():
        for comp in ("app", "bff"):
            test_dir = BASE_DIR / "src" / comp / "tests"
            if test_dir.exists():
                for f in test_dir.glob("test_pbi_red_*.py"):
                    f.unlink()
                for f in (BASE_DIR / "src" / comp).glob("pbi_red_*_service.py"):
                    f.unlink()

    _clean()
    yield
    _clean()


@pytest.mark.asyncio
async def test_red_phase_rejects_syntax_error():
    """Redフェーズでテストコードに構文エラーがある場合、Greenに進まずRED_PHASE_EXECUTION_ERRORとなること (Issue #12)"""
    pbi_id = "PBI-RED-001"
    component = "app"
    design_output = {"design_status": "COMPLETED"}

    syntax_err_out = (
        1,
        "",
        'File "test_pbi_red_001.py", line 4\n    def test_foo(\n                ^\nSyntaxError: invalid syntax',
    )
    with patch("orchestrator.activities._run_pytest_on_target", new_callable=AsyncMock) as mock_pytest:
        mock_pytest.return_value = syntax_err_out
        result = await run_tdd_cycle_activity(pbi_id, component, design_output)
        assert result.get("tdd_status") == "RED_PHASE_EXECUTION_ERROR"
        assert "SYNTAX_ERROR" in result.get("error", "")


@pytest.mark.asyncio
async def test_red_phase_rejects_collection_error():
    """Redフェーズでテスト収集エラー (ModuleNotFoundError/ImportError等) が発生した場合、Greenに進まないこと (Issue #12)"""
    pbi_id = "PBI-RED-002"
    component = "app"
    design_output = {"design_status": "COMPLETED"}

    collection_err_out = (
        2,
        "ERROR collecting tests/test_pbi_red_002.py\nModuleNotFoundError: No module named 'invalid_pkg'",
        "",
    )
    with patch("orchestrator.activities._run_pytest_on_target", new_callable=AsyncMock) as mock_pytest:
        mock_pytest.return_value = collection_err_out
        result = await run_tdd_cycle_activity(pbi_id, component, design_output)
        assert result.get("tdd_status") == "RED_PHASE_EXECUTION_ERROR"
        assert "COLLECTION_ERROR" in result.get("error", "")


@pytest.mark.asyncio
async def test_red_phase_rejects_timeout_error():
    """Redフェーズでpytestがタイムアウトした場合、Greenに進まずタイムアウトとして中断されること (Issue #12, #19)"""
    pbi_id = "PBI-RED-003"
    component = "app"
    design_output = {"design_status": "COMPLETED"}

    timeout_out = (-1, "", "TimeoutError: pytest execution timed out after 30.0 seconds")
    with patch("orchestrator.activities._run_pytest_on_target", new_callable=AsyncMock) as mock_pytest:
        mock_pytest.return_value = timeout_out
        result = await run_tdd_cycle_activity(pbi_id, component, design_output)
        assert result.get("tdd_status") == "RED_PHASE_EXECUTION_ERROR"
        assert "TIMEOUT_ERROR" in result.get("error", "")


@pytest.mark.asyncio
async def test_red_phase_rejects_regression_failure():
    """新規テストが失敗しても、既存の回帰テストが失敗した場合はRed成功とみなさないこと (Issue #12)"""
    pbi_id = "PBI-RED-004"
    component = "app"
    design_output = {"design_status": "COMPLETED"}

    # 1回目の呼び出し (新規テスト): 期待通りアサーション失敗
    # 2回目の呼び出し (既存テスト): 予期せず失敗
    new_test_fail = (1, "FAILED tests/test_pbi_red_004.py::test_foo - AssertionError: assert 404 == 200", "")
    existing_test_fail = (1, "FAILED tests/test_legacy.py::test_bar - AssertionError: assert False", "")

    with (
        patch("orchestrator.activities._run_pytest_on_target", new_callable=AsyncMock) as mock_pytest,
        patch("pathlib.Path.glob") as mock_glob,
    ):
        mock_pytest.side_effect = [new_test_fail, existing_test_fail]
        # 既存テストファイルが存在する状況をモック
        from pathlib import Path

        mock_existing = Path("src/app/tests/test_existing.py")
        mock_glob.return_value = [mock_existing]

        with patch("pathlib.Path.is_file", return_value=True):
            result = await run_tdd_cycle_activity(pbi_id, component, design_output)
            assert result.get("tdd_status") == "REGRESSION_FAILURE"
            assert "REGRESSION_FAILURE" in result.get("tdd_status", "")


@pytest.mark.asyncio
async def test_red_phase_rejects_import_error_in_test_execution():
    """実行時に ImportError が発生した場合、偽の Red 成功とせず RED_PHASE_EXECUTION_ERROR とすること"""
    pbi_id = "PBI-RED-005"
    component = "app"
    design_output = {"design_status": "COMPLETED"}

    import_err_out = (
        1,
        "FAILED tests/test_pbi_red_005.py::test_foo\nImportError: cannot import name 'PbiRed005Service'",
        "",
    )
    with patch("orchestrator.activities._run_pytest_on_target", new_callable=AsyncMock) as mock_pytest:
        mock_pytest.return_value = import_err_out
        result = await run_tdd_cycle_activity(pbi_id, component, design_output)
        assert result.get("tdd_status") == "RED_PHASE_EXECUTION_ERROR"
        assert "COLLECTION_ERROR" in result.get("error", "")
