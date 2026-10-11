import os
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from orchestrator.activities import _strip_markdown_codeblock, decompose_specs_activity


@pytest.mark.asyncio
async def test_fallback_decomposition_extracts_biz_rules(tmp_path):
    # テスト用の一時 Example Map
    sample_map = """# Example Map: PBI-999

## PBI 要件概要
会員登録機能の仕様確認

## BIZ-001: メールアドレス形式のバリデーション
| ルール | 具体例 (Example) | 期待される結果 |
| :--- | :--- | :--- |
| RFC準拠 | test@example.com | 成功 |

## BIZ-002: 重複メールアドレスの拒否
| ルール | 具体例 (Example) | 期待される結果 |
| :--- | :--- | :--- |
| 既存登録済み | dup@example.com | 409 Conflict |
"""
    map_file = tmp_path / "PBI-999.md"
    map_file.write_text(sample_map, encoding="utf-8")

    # API キーを無効化してフォールバックパスを通す
    with patch.dict(os.environ, {"GEMINI_API_KEY": "", "GOOGLE_API_KEY": ""}):
        with patch("orchestrator.activities._call_gemini_for_decomposition", new_callable=AsyncMock) as mock_gemini:
            mock_gemini.return_value = None

            result = await decompose_specs_activity("PBI-999", str(map_file))

            assert "business" in result
            assert "app" in result
            assert "bff" in result

            biz_content = Path(result["business"]).read_text(encoding="utf-8")
            app_content = Path(result["app"]).read_text(encoding="utf-8")
            bff_content = Path(result["bff"]).read_text(encoding="utf-8")

            # 宣言とタグの検証
            assert "# language: ja" in biz_content
            assert "@BIZ-001" in biz_content
            assert "@BIZ-002" in biz_content

            # App/BFF層に @covers タグが付与されていること
            assert "@covers:BIZ-001" in app_content
            assert "@covers:BIZ-002" in app_content
            assert "@covers:BIZ-001" in bff_content
            assert "@covers:BIZ-002" in bff_content


@pytest.mark.asyncio
async def test_gemini_decomposition_output(tmp_path):
    sample_map = """# Example Map: PBI-888\n## BIZ-001: ログイン\n"""
    map_file = tmp_path / "PBI-888.md"
    map_file.write_text(sample_map, encoding="utf-8")

    mock_llm_response = {
        "business": "# language: ja\n機能: PBI-888 LLM生成ビジネス仕様\n  @BIZ-001\n  シナリオ: 成功",
        "app": "# language: ja\n@covers:BIZ-001\n機能: PBI-888 UI仕様",
        "bff": "# language: ja\n@covers:BIZ-001\n機能: PBI-888 API仕様",
    }

    with patch("orchestrator.activities._call_gemini_for_decomposition", new_callable=AsyncMock) as mock_gemini:
        mock_gemini.return_value = mock_llm_response

        result = await decompose_specs_activity("PBI-888", str(map_file))

        biz_content = Path(result["business"]).read_text(encoding="utf-8")
        app_content = Path(result["app"]).read_text(encoding="utf-8")
        bff_content = Path(result["bff"]).read_text(encoding="utf-8")

        assert biz_content == mock_llm_response["business"]
        assert app_content == mock_llm_response["app"]
        assert bff_content == mock_llm_response["bff"]


def test_strip_markdown_codeblock():
    raw_json = '```json\n{"key": "value"}\n```'
    assert _strip_markdown_codeblock(raw_json) == '{"key": "value"}'

    plain = '{"key": "value"}'
    assert _strip_markdown_codeblock(plain) == '{"key": "value"}'
