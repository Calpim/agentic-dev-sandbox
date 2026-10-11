import os
import pytest
import yaml
from pathlib import Path
from unittest.mock import AsyncMock, patch

from orchestrator.activities import draft_schema_activity, _fallback_draft_openapi

@pytest.mark.asyncio
async def test_fallback_draft_openapi(tmp_path):
    pbi_id = "PBI-777"
    bff_file = tmp_path / f"{pbi_id}.feature"
    bff_file.write_text("""# language: ja
@covers:BIZ-001
機能: PBI-777 API連携
  シナリオ: 正常リクエスト
    もし POSTリクエストが送信される
    ならば ステータス200が返る
""", encoding="utf-8")

    decomp_result = {
        "bff": str(bff_file),
        "business": "",
        "app": "",
    }

    # APIキー無効化でフォールバックパスを検証
    with patch.dict(os.environ, {"GEMINI_API_KEY": "", "GOOGLE_API_KEY": ""}):
        with patch("orchestrator.activities._call_gemini_for_openapi", new_callable=AsyncMock) as mock_gemini:
            mock_gemini.return_value = None

            schema_path = await draft_schema_activity(pbi_id, decomp_result)
            assert Path(schema_path).exists()

            content = Path(schema_path).read_text(encoding="utf-8")
            # YAML構文として妥当であること
            parsed = yaml.safe_load(content)
            assert parsed["openapi"] == "3.0.3"
            assert f"/api/v1/{pbi_id.lower()}" in parsed["paths"]
            
            post_op = parsed["paths"][f"/api/v1/{pbi_id.lower()}"]["post"]
            assert "200" in post_op["responses"]
            assert "400" in post_op["responses"]
            assert "409" in post_op["responses"]

@pytest.mark.asyncio
async def test_feedback_reflected_in_fallback_schema(tmp_path):
    pbi_id = "PBI-777"
    decomp_result = {"bff": "", "business": "", "app": ""}
    feedback = "パスワード最小文字数8文字のバリデーションを追加"

    with patch.dict(os.environ, {"GEMINI_API_KEY": "", "GOOGLE_API_KEY": ""}):
        with patch("orchestrator.activities._call_gemini_for_openapi", new_callable=AsyncMock) as mock_gemini:
            mock_gemini.return_value = None

            schema_path = await draft_schema_activity(pbi_id, decomp_result, feedback=feedback)
            content = Path(schema_path).read_text(encoding="utf-8")

            assert feedback in content
            parsed = yaml.safe_load(content)
            assert parsed["openapi"] == "3.0.3"

@pytest.mark.asyncio
async def test_gemini_openapi_draft_mock(tmp_path):
    pbi_id = "PBI-666"
    decomp_result = {"bff": "", "business": "", "app": ""}
    mock_yaml = """openapi: 3.0.3
info:
  title: Mocked PBI-666 API
  version: 1.0.0
paths:
  /api/v1/pbi-666:
    post:
      summary: LLM generated endpoint
      responses:
        '200':
          description: OK
"""

    with patch("orchestrator.activities._call_gemini_for_openapi", new_callable=AsyncMock) as mock_gemini:
        mock_gemini.return_value = mock_yaml

        schema_path = await draft_schema_activity(pbi_id, decomp_result)
        content = Path(schema_path).read_text(encoding="utf-8")
        assert content == mock_yaml
        parsed = yaml.safe_load(content)
        assert parsed["info"]["title"] == "Mocked PBI-666 API"
