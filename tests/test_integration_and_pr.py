import os
import shutil
import pytest
from pathlib import Path
from unittest.mock import patch, MagicMock

from orchestrator.activities import (
    BASE_DIR,
    integration_and_pr_activity,
    _build_pr_body,
    _create_github_pr,
    _generate_integration_test_code,
)

@pytest.fixture(autouse=True)
def cleanup_integration_artifacts():
    yield
    for pbi in ["PBI-801", "PBI-802", "PBI-803"]:
        clean_id = pbi.lower().replace("-", "_")
        integ_test = BASE_DIR / "src" / "integration" / "tests" / f"test_{clean_id}_integration.py"
        if integ_test.exists():
            integ_test.unlink()
        
        # サービスのクリーンアップ
        for comp in ("app", "bff"):
            impl = BASE_DIR / "src" / comp / f"{clean_id}_service.py"
            if impl.exists():
                impl.unlink()

        task_dir = BASE_DIR / "tasks" / pbi
        if task_dir.exists():
            shutil.rmtree(task_dir, ignore_errors=True)

@pytest.mark.asyncio
async def test_integration_and_pr_activity_success():
    pbi_id = "PBI-801"
    clean_id = pbi_id.lower().replace("-", "_")

    # App と BFF の実装を事前に配置（結合対象）
    app_dir = BASE_DIR / "src" / "app"
    bff_dir = BASE_DIR / "src" / "bff"
    app_dir.mkdir(parents=True, exist_ok=True)
    bff_dir.mkdir(parents=True, exist_ok=True)

    app_service_code = """from typing import Dict, Any
class Pbi801Service:
    def execute(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        return {"success": True, "status_code": 200, "identifier": payload.get("identifier")}
"""
    (app_dir / f"{clean_id}_service.py").write_text(app_service_code, encoding="utf-8")
    (bff_dir / f"{clean_id}_service.py").write_text(app_service_code, encoding="utf-8")

    app_res = {"status": "SUCCESS", "rule_guard": {"category": "SUCCESS"}}
    bff_res = {"status": "SUCCESS", "rule_guard": {"category": "SUCCESS"}}

    with patch("orchestrator.activities._create_github_pr", return_value="https://github.com/Calpim/agentic-dev-sandbox/pull/999"):
        result = await integration_and_pr_activity(pbi_id, app_res, bff_res)

        assert result.get("status") == "COMPLETED"
        assert result.get("pbi_id") == pbi_id
        assert result.get("integration_status") == "ALL_GREEN"
        assert result.get("pr_url") == "https://github.com/Calpim/agentic-dev-sandbox/pull/999"

        summary_file = Path(result.get("pr_summary_path"))
        assert summary_file.exists()
        content = summary_file.read_text(encoding="utf-8")
        assert f"PBI `{pbi_id}`" in content
        assert "トレーサビリティ対応表" in content
        assert "Integration Gate 合格" in content

def test_build_pr_body_traceability_table(tmp_path):
    pbi_id = "PBI-802"
    em_file = BASE_DIR / "specs" / "example-maps" / f"{pbi_id}.md"
    em_file.parent.mkdir(parents=True, exist_ok=True)
    em_file.write_text("""# Example Map: PBI-802
## BIZ-001: 正常ログイン
## BIZ-002: アカウントロックアウト
""", encoding="utf-8")

    app_res = {"rule_guard": {"category": "SUCCESS"}}
    bff_res = {"rule_guard": {"category": "SUCCESS"}}
    integration_res = {"rule_guard": {"category": "SUCCESS"}}

    body = _build_pr_body(pbi_id, app_res, bff_res, integration_res)

    assert "`BIZ-001`" in body
    assert "正常ログイン" in body
    assert "`BIZ-002`" in body
    assert "アカウントロックアウト" in body
    assert "@covers:BIZ-001" in body
    assert "@covers:BIZ-002" in body

    # 後始末
    if em_file.exists():
        em_file.unlink()

def test_create_github_pr_offline_fallback():
    # トークンなしの場合、クラッシュせず None を返す
    with patch("orchestrator.activities._get_github_token", return_value=None):
        url = _create_github_pr("PBI-803", "title", "body")
        assert url is None

def test_create_github_pr_mock():
    mock_resp_data = {"html_url": "https://github.com/Calpim/agentic-dev-sandbox/pull/123"}
    mock_resp = MagicMock()
    mock_resp.read.return_value = '{"html_url": "https://github.com/Calpim/agentic-dev-sandbox/pull/123"}'.encode("utf-8")
    mock_resp.__enter__.return_value = mock_resp

    with patch("orchestrator.activities._get_github_token", return_value="dummy-token"):
        with patch("orchestrator.activities._get_current_branch", return_value="feature/test"):
            with patch("urllib.request.urlopen", return_value=mock_resp):
                url = _create_github_pr("PBI-803", "title", "body")
                assert url == "https://github.com/Calpim/agentic-dev-sandbox/pull/123"
