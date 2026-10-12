import os
from unittest.mock import AsyncMock, patch

import pytest

from orchestrator.activities import (
    _generate_impl_code,
    decompose_specs_activity,
)
from orchestrator.guards.validators import (
    extract_biz_ids_from_example_map,
    validate_gherkin_coverage,
)


def test_extract_biz_ids_from_example_map():
    sample = """# Example Map: PBI-101
## BIZ-001: 正常入力
## BIZ-002: 重複エラー
## BIZ-001: 重複記載
## BIZ-003: 境界値
"""
    biz_ids = extract_biz_ids_from_example_map(sample)
    assert biz_ids == ["BIZ-001", "BIZ-002", "BIZ-003"]


def test_validate_gherkin_coverage_success():
    sample_map = """# Example Map: PBI-101
## BIZ-001: 正常入力
## BIZ-002: 重複エラー
"""
    biz_gherkin = """# language: ja
機能: PBI-101
  @BIZ-001
  シナリオ: 1
  @BIZ-002
  シナリオ: 2
"""
    app_gherkin = """# language: ja
@covers:BIZ-001 @covers:BIZ-002
機能: PBI-101 UI
"""
    bff_gherkin = """# language: ja
@covers:BIZ-001 @covers:BIZ-002
機能: PBI-101 API
"""
    res = validate_gherkin_coverage(sample_map, biz_gherkin, app_gherkin, bff_gherkin, strict=True)
    assert res["is_covered"] is True
    assert res["missing_biz"] == []
    assert res["missing_app"] == []
    assert res["missing_bff"] == []


def test_validate_gherkin_coverage_missing_and_fail_closed():
    sample_map = """# Example Map: PBI-101
## BIZ-001: 正常入力
## BIZ-002: 重複エラー
"""
    biz_gherkin = """# language: ja
機能: PBI-101
  @BIZ-001
  シナリオ: 1
"""
    app_gherkin = """# language: ja
@covers:BIZ-001
機能: PBI-101 UI
"""
    bff_gherkin = """# language: ja
@covers:BIZ-001
機能: PBI-101 API
"""
    # Non-strict mode: returns coverage dict without error
    res = validate_gherkin_coverage(sample_map, biz_gherkin, app_gherkin, bff_gherkin, strict=False)
    assert res["is_covered"] is False
    assert res["missing_biz"] == ["BIZ-002"]
    assert res["missing_app"] == ["BIZ-002"]
    assert res["missing_bff"] == ["BIZ-002"]

    # Strict mode: raises ValueError (fail-closed)
    with pytest.raises(ValueError, match="Gherkin specification coverage validation failed"):
        validate_gherkin_coverage(sample_map, biz_gherkin, app_gherkin, bff_gherkin, strict=True)


@pytest.mark.asyncio
async def test_decompose_specs_strict_mode_fail_closed(tmp_path):
    sample_map = """# Example Map: PBI-102
## BIZ-001: 正常
## BIZ-002: 異常
"""
    map_file = tmp_path / "PBI-102.md"
    map_file.write_text(sample_map, encoding="utf-8")

    # LLM returns incomplete Gherkin missing BIZ-002
    incomplete_llm = {
        "business": "# language: ja\n機能: PBI-102\n  @BIZ-001\n  シナリオ: ok",
        "app": "# language: ja\n@covers:BIZ-001\n機能: App",
        "bff": "# language: ja\n@covers:BIZ-001\n機能: BFF",
    }

    with patch.dict(os.environ, {"STRICT_SPEC_GENERATION": "1"}):
        with patch("orchestrator.activities._call_gemini_for_decomposition", new_callable=AsyncMock) as mock_llm:
            mock_llm.return_value = incomplete_llm
            with pytest.raises(ValueError, match="coverage validation failed"):
                await decompose_specs_activity("PBI-102", str(map_file))


def test_generate_impl_code_reflects_ddd_entity_and_passes():
    design_doc = """# Architecture & DDD Design: PBI-103 (APP)
## 1. 責務と概要
PBI-103 のユーザー情報登録サービス設計。
## 2. クラス・サービス設計
- Entity: Pbi103Entity
- Service: Pbi103Service
- Repository: In-memory store
"""
    code = _generate_impl_code("PBI-103", "app", design_content=design_doc)
    assert "class Pbi103Entity:" in code
    assert "class Pbi103Service:" in code
    assert "Architecture & Domain Model reflected from design" in code

    # Execute generated code to verify functionality
    from typing import Any, Dict

    namespace: Dict[str, Any] = {}
    exec(code, namespace)

    entity_cls = namespace["Pbi103Entity"]
    service_cls = namespace["Pbi103Service"]

    entity = entity_cls(identifier="test-id", component="app")
    assert entity.identifier == "test-id"
    assert entity.to_dict()["identifier"] == "test-id"

    service = service_cls()
    # 正常系
    res = service.execute({"identifier": "test-01"})
    assert res["success"] is True
    assert res["status_code"] == 200
    assert res["identifier"] == "test-01"
    assert res["data"]["identifier"] == "test-01"

    # 保存されたエンティティの取得
    saved_entity = service.get_entity("test-01")
    assert saved_entity is not None
    assert saved_entity.identifier == "test-01"

    # 異常系 (必須バリデーション)
    with pytest.raises(ValueError, match="identifier is required"):
        service.execute({})


def test_gherkin_coverage_exact_tag_boundary():
    """タグ照合が部分一致ではなく単語境界完全一致で行われること (BIZ-1 と BIZ-10 の誤認防止)"""
    sample_map = """# Example Map: PBI-101
## BIZ-1: 正常入力
"""
    # Gherkin 側に @BIZ-10 や @covers:BIZ-10 しかない場合
    biz_gherkin = """# language: ja
機能: PBI-101
  @BIZ-10
  シナリオ: 10
"""
    app_gherkin = """# language: ja
@covers:BIZ-10
機能: PBI-101 UI
"""
    bff_gherkin = """# language: ja
@covers:BIZ-10
機能: PBI-101 API
"""
    # BIZ-1 は網羅されていないため missing として検出されなければならない
    res = validate_gherkin_coverage(sample_map, biz_gherkin, app_gherkin, bff_gherkin, strict=False)
    assert res["is_covered"] is False
    assert "BIZ-1" in res["missing_biz"]
    assert "BIZ-1" in res["missing_app"]
    assert "BIZ-1" in res["missing_bff"]

    # strict=True 時は ValueError
    with pytest.raises(ValueError, match="coverage validation failed"):
        validate_gherkin_coverage(sample_map, biz_gherkin, app_gherkin, bff_gherkin, strict=True)
