from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from orchestrator.activities import draft_schema_activity
from orchestrator.guards.task_state import (
    get_task_state,
    update_pipeline_phase_atomic,
    update_task_state,
)
from orchestrator.guards.validators import validate_openapi_spec


def test_concurrent_state_updates(tmp_path: Path):
    """
    Issue #15: tasks/{pbi_id}/state.json の並行排他制御検証
    複数のスレッドから同時に app / bff の状態を並行更新しても
    データの欠損や JSON 破損が発生せず、アトミックに反映されることを検証する。
    """
    pbi_id = "PBI-CONCURRENT-001"

    # 初期化
    def _init(data):
        data["pbi"] = pbi_id
        data["pipelines"] = {
            "app": {"phase": "INIT", "status": "PLANNED"},
            "bff": {"phase": "INIT", "status": "PLANNED"},
        }

    update_task_state(pbi_id, tmp_path, _init)

    # 20個の並行ワーカーで交互に状態を更新
    def worker(idx: int):
        comp = "app" if idx % 2 == 0 else "bff"
        phase = f"PHASE_{idx}"
        status = "COMPLETED" if idx % 3 == 0 else "IN_PROGRESS"
        update_pipeline_phase_atomic(pbi_id, tmp_path, comp, phase, status)

    with ThreadPoolExecutor(max_workers=8) as executor:
        futures = [executor.submit(worker, i) for i in range(20)]
        for f in futures:
            f.result()

    final_state = get_task_state(pbi_id, tmp_path)
    assert final_state["pbi"] == pbi_id
    assert "app" in final_state["pipelines"]
    assert "bff" in final_state["pipelines"]
    # 最終更新値が正常に保持されていること
    assert final_state["pipelines"]["app"]["phase"].startswith("PHASE_")
    assert final_state["pipelines"]["bff"]["phase"].startswith("PHASE_")


def test_validate_openapi_spec_valid():
    """Issue #17: 正常な OpenAPI 3.0.3 スキーマの検証"""
    valid_yaml = """openapi: 3.0.3
info:
  title: Test API
  version: 1.0.0
paths:
  /api/v1/resource:
    post:
      summary: Create resource
      responses:
        '200':
          description: OK
"""
    res = validate_openapi_spec(valid_yaml, strict=True)
    assert res["is_valid"] is True
    assert res["errors"] == []
    assert res["schema"]["openapi"] == "3.0.3"


def test_validate_openapi_spec_invalid_cases():
    """Issue #17: 不正な OpenAPI スキーマの拒否と fail-closed"""
    # 1. 構文不正
    res = validate_openapi_spec("invalid: yaml: [", strict=False)
    assert res["is_valid"] is False

    # 2. バージョン不正 (Swagger 2.0)
    res = validate_openapi_spec(
        """openapi: 2.0
info: {title: Test, version: '1.0'}
paths:
  /test:
    get:
      responses: {'200': {description: ok}}
""",
        strict=False,
    )
    assert res["is_valid"] is False
    assert any("Unsupported OpenAPI version" in e for e in res["errors"])

    # 3. info.title 欠落
    res = validate_openapi_spec(
        """openapi: 3.0.3
info: {version: '1.0'}
paths:
  /test:
    get:
      responses: {'200': {description: ok}}
""",
        strict=False,
    )
    assert res["is_valid"] is False
    assert any("info.title" in e for e in res["errors"])

    # 4. パスが '/' で始まらない
    res = validate_openapi_spec(
        """openapi: 3.0.3
info: {title: Test, version: '1.0'}
paths:
  api/v1/invalid:
    get:
      responses: {'200': {description: ok}}
""",
        strict=False,
    )
    assert res["is_valid"] is False
    assert any("must start with '/'" in e for e in res["errors"])

    # 5. responses 欠落 (strict=True で例外送出)
    with pytest.raises(ValueError, match="OpenAPI specification validation failed"):
        validate_openapi_spec(
            """openapi: 3.0.3
info: {title: Test, version: '1.0'}
paths:
  /test:
    get:
      summary: Missing responses
""",
            strict=True,
        )


@pytest.mark.asyncio
async def test_draft_schema_rejects_invalid_llm_schema(tmp_path: Path):
    """Issue #17: draft_schema_activity が不正な LLM 出力を拒否して安全にフォールバックすること"""
    pbi_id = "PBI-SCHEMA-GUARD-01"
    decomp = {"bff": "", "business": "", "app": ""}

    # 不正な OpenAPI (paths 内に responses なし)
    bad_llm_schema = """openapi: 3.0.3
info:
  title: Bad Schema
  version: 1.0.0
paths:
  /api/v1/test:
    post:
      summary: No responses defined
"""
    with patch("orchestrator.activities._call_gemini_for_openapi", new_callable=AsyncMock) as mock_llm:
        mock_llm.return_value = bad_llm_schema
        schema_path = await draft_schema_activity(pbi_id, decomp)
        assert Path(schema_path).exists()

        content = Path(schema_path).read_text(encoding="utf-8")
        # 不正なLLM出力は拒否され、フォールバックの正しい OpenAPI 3.0.3 が生成されていること
        res = validate_openapi_spec(content, strict=True)
        assert res["is_valid"] is True
        assert f"/api/v1/{pbi_id.lower()}" in res["schema"]["paths"]
