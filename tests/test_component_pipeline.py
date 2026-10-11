import os
import pytest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from orchestrator.activities import (
    run_design_activity,
    run_tdd_cycle_activity,
    _fallback_design_doc,
)

@pytest.mark.asyncio
async def test_run_design_activity_fallback(tmp_path):
    pbi_id = "PBI-501"
    component = "app"

    # API キーを無効化してフォールバック検証
    with patch.dict(os.environ, {"GEMINI_API_KEY": "", "GOOGLE_API_KEY": ""}):
        with patch("orchestrator.activities._call_gemini_for_design", new_callable=AsyncMock) as mock_gemini:
            mock_gemini.return_value = None

            result = await run_design_activity(pbi_id, component)
            assert result.get("design_status") == "COMPLETED"
            assert result.get("component") == component
            assert Path(result.get("design_path")).exists()

            content = Path(result.get("design_path")).read_text(encoding="utf-8")
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
        content = Path(result.get("design_path")).read_text(encoding="utf-8")
        assert content == mock_design

@pytest.mark.asyncio
async def test_run_tdd_cycle_activity_execution():
    pbi_id = "PBI-503"
    component = "bff"
    design_output = {"design_status": "COMPLETED"}

    result = await run_tdd_cycle_activity(pbi_id, component, design_output)

    assert result.get("tdd_status") == "ALL_GREEN"
    assert result.get("component") == component
    assert Path(result.get("test_file")).exists()
    assert Path(result.get("impl_file")).exists()

    rule_guard_data = result.get("rule_guard", {})
    assert rule_guard_data.get("category") == "SUCCESS"
    assert rule_guard_data.get("next_action") == "PROCEED"
