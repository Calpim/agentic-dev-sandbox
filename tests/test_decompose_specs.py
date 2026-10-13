import os
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from orchestrator.activities import (
    BASE_DIR,
    _inject_gherkin_tags,
    _strip_markdown_codeblock,
    decompose_specs_activity,
)


@pytest.fixture(autouse=True)
def cleanup_specs_artifacts():
    pbi_ids = ["PBI-777", "PBI-888", "PBI-999"]

    def _clean():
        for pbi in pbi_ids:
            for comp in ("business", "app", "bff"):
                f = BASE_DIR / "specs" / "features" / comp / f"{pbi}.feature"
                if f.exists():
                    f.unlink()

    _clean()
    yield
    _clean()


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


def test_inject_gherkin_tags_preserves_language_header():
    """Gherkin にタグを挿入する際、# language: ja ヘッダが最先頭に保持され、タグが機能の直前に配置されること"""
    content_with_header = """# language: ja
機能: 会員管理
  シナリオ: 登録成功
"""
    result = _inject_gherkin_tags(content_with_header, ["@covers:BIZ-001", "@covers:BIZ-002"])
    lines = [line.strip() for line in result.splitlines() if line.strip()]
    assert lines[0] == "# language: ja"
    assert lines[1] == "@covers:BIZ-001"
    assert lines[2] == "@covers:BIZ-002"
    assert lines[3] == "機能: 会員管理"

    # 言語ヘッダが無い場合は最先頭にタグが配置されること
    content_without_header = """機能: 会員管理
  シナリオ: 登録成功
"""
    res_no_header = _inject_gherkin_tags(content_without_header, ["@covers:BIZ-001"])
    lines_no_header = [line.strip() for line in res_no_header.splitlines() if line.strip()]
    assert lines_no_header[0] == "@covers:BIZ-001"
    assert lines_no_header[1] == "機能: 会員管理"


@pytest.mark.asyncio
async def test_gemini_decomposition_auto_completes_tags_preserving_header(tmp_path):
    """LLM 出力にタグ不足があった場合、# language: ja ヘッダを壊さずにタグが自動補完されること"""
    sample_map = """# Example Map: PBI-777
## BIZ-001: ログイン
## BIZ-002: ログアウト
"""
    map_file = tmp_path / "PBI-777.md"
    map_file.write_text(sample_map, encoding="utf-8")

    # LLM 出力が BIZ-002 の covers タグを欠落させているケース
    mock_llm_response = {
        "business": "# language: ja\n機能: PBI-777 ビジネス\n  @BIZ-001\n  シナリオ: ログイン\n  @BIZ-002\n  シナリオ: ログアウト",
        "app": "# language: ja\n@covers:BIZ-001\n機能: PBI-777 UI",
        "bff": "# language: ja\n@covers:BIZ-001\n機能: PBI-777 API",
    }

    with patch("orchestrator.activities._call_gemini_for_decomposition", new_callable=AsyncMock) as mock_gemini:
        mock_gemini.return_value = mock_llm_response

        result = await decompose_specs_activity("PBI-777", str(map_file))

        app_content = Path(result["app"]).read_text(encoding="utf-8")
        bff_content = Path(result["bff"]).read_text(encoding="utf-8")

        # 先頭行は # language: ja のままであること
        assert app_content.startswith("# language: ja")
        assert bff_content.startswith("# language: ja")

        # 欠落していた @covers:BIZ-002 が補完されていること
        assert "@covers:BIZ-002" in app_content
        assert "@covers:BIZ-002" in bff_content
