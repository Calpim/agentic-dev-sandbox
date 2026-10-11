import os
import shutil
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from orchestrator.activities import (
    BASE_DIR,
    _call_gemini_for_bdd_steps,
    _generate_bdd_step_code,
    _generate_integration_bdd_test_code,
    integration_and_pr_activity,
    run_tdd_cycle_activity,
)


@pytest.fixture(autouse=True)
def cleanup_bdd_test_artifacts():
    """BDD 外側ループテスト用の一時ファイルをクリーンアップ"""
    test_pbis = ["PBI-BDD-001", "PBI-BDD-002", "PBI-BDD-003"]

    def _clean():
        for pbi in test_pbis:
            clean_id = pbi.lower().replace("-", "_")
            for comp in ("app", "bff"):
                test_file = BASE_DIR / "src" / comp / "tests" / f"test_{clean_id}.py"
                bdd_file = BASE_DIR / "src" / comp / "tests" / f"test_{clean_id}_bdd.py"
                impl_file = BASE_DIR / "src" / comp / f"{clean_id}_service.py"
                feat_file = BASE_DIR / "specs" / "features" / comp / f"{pbi}.feature"
                for f in (test_file, bdd_file, impl_file, feat_file):
                    if f.exists():
                        f.unlink()

            biz_feat = BASE_DIR / "specs" / "features" / "business" / f"{pbi}.feature"
            integ_unit = BASE_DIR / "src" / "integration" / "tests" / f"test_{clean_id}_integration.py"
            integ_bdd = BASE_DIR / "src" / "integration" / "tests" / f"test_{clean_id}_bdd_integration.py"
            for f in (biz_feat, integ_unit, integ_bdd):
                if f.exists():
                    f.unlink()

            task_dir = BASE_DIR / "tasks" / pbi
            if task_dir.exists():
                shutil.rmtree(task_dir, ignore_errors=True)

        integ_dir = BASE_DIR / "src" / "integration"
        if integ_dir.exists():
            shutil.rmtree(integ_dir, ignore_errors=True)

    _clean()
    yield
    _clean()


def test_generate_bdd_step_code():
    """_generate_bdd_step_code が有効な pytest-bdd Step Definition を生成すること"""
    pbi_id = "PBI-BDD-001"
    component = "app"

    code = _generate_bdd_step_code(pbi_id, component, directive_instruction="Fix test key")
    assert "from pytest_bdd import scenarios, given, when, then, parsers" in code
    assert "PbiBdd001Service" in code
    assert "FEATURE_FILE = Path(" in code and "specs/features/app/PBI-BDD-001.feature" in code
    assert "@given" in code
    assert "@when" in code
    assert "@then" in code
    assert "Fix test key" in code


@pytest.mark.asyncio
async def test_call_gemini_for_bdd_steps_mock():
    """Gemini API を用いた BDD Step Definition 生成のモックテスト"""
    mock_steps = "# Generated BDD steps\nfrom pytest_bdd import scenarios\nscenarios('test.feature')"

    mock_client = MagicMock()
    mock_response = MagicMock()
    mock_response.text = f"```python\n{mock_steps}\n```"
    mock_client.aio.models.generate_content = AsyncMock(return_value=mock_response)

    with patch.dict(os.environ, {"GEMINI_API_KEY": "fake_key"}):
        with patch("google.genai.Client", return_value=mock_client):
            result = await _call_gemini_for_bdd_steps("PBI-BDD-001", "app", "Feature: Test")
            assert result == mock_steps


def test_generate_integration_bdd_test_code():
    """_generate_integration_bdd_test_code がビジネス Gherkin を駆動するコードを生成すること"""
    pbi_id = "PBI-BDD-001"
    code = _generate_integration_bdd_test_code(pbi_id)

    assert "from pytest_bdd import scenarios, given, when, then, parsers" in code
    assert "BIZ_FEATURE_FILE = Path(" in code and "specs/features/business/PBI-BDD-001.feature" in code
    assert "AppService" in code
    assert "BffService" in code
    assert "integ_bdd_context" in code


@pytest.mark.asyncio
async def test_run_tdd_cycle_with_outer_bdd_loop():
    """Phase 4-2 & 5: Feature ファイルが存在する場合、内側ユニット + 外側 BDD の二重ループ TDD が完遂すること"""
    pbi_id = "PBI-BDD-002"
    component = "app"

    # 1. Feature ファイルを事前に配置
    feat_dir = BASE_DIR / "specs" / "features" / component
    feat_dir.mkdir(parents=True, exist_ok=True)
    feat_file = feat_dir / f"{pbi_id}.feature"
    feat_file.write_text(
        """# language: ja
@covers:BIZ-001
機能: PBI-BDD-002 クライアントUI振る舞い
  シナリオ: 画面操作と状態更新
    前提 画面が表示されている
    もし 操作を実行する
    ならば 画面に結果が適切に反映される
""",
        encoding="utf-8",
    )

    design_output = {"design_status": "COMPLETED"}

    # 2. 二重ループ TDD を実行 (オフライン決定論的モードで検証)
    with patch.dict(os.environ, {"GEMINI_API_KEY": "", "GOOGLE_API_KEY": ""}):
        result = await run_tdd_cycle_activity(pbi_id, component, design_output)

    assert result["tdd_status"] == "ALL_GREEN"
    assert result["component"] == component
    assert Path(result["test_file"]).exists()
    assert Path(result["bdd_test_file"]).exists()
    assert Path(result["impl_file"]).exists()

    rule_guard = result.get("rule_guard", {})
    assert rule_guard.get("category") == "SUCCESS"
    assert rule_guard.get("next_action") == "PROCEED"


@pytest.mark.asyncio
async def test_run_tdd_cycle_with_outer_bdd_loop_gemini_mock():
    """Phase 4-2 & 5: Gemini API による BDD 生成コードを含む二重ループ TDD の検証"""
    pbi_id = "PBI-BDD-002"
    component = "bff"
    clean_id = pbi_id.lower().replace("-", "_")

    feat_dir = BASE_DIR / "specs" / "features" / component
    feat_dir.mkdir(parents=True, exist_ok=True)
    feat_file = feat_dir / f"{pbi_id}.feature"
    feat_file.write_text(
        """# language: ja
@covers:BIZ-001
機能: PBI-BDD-002 APIエンドポイント連携
  シナリオ: APIリクエスト処理
    もし APIリクエストが送信される
    ならば 正常に完了する
""",
        encoding="utf-8",
    )

    mock_bdd_code = f"""# -*- coding: utf-8 -*-
from pathlib import Path
import pytest
from pytest_bdd import scenarios, given, when, then, parsers
from src.bff.{clean_id}_service import PbiBdd002Service

FEATURE_FILE = Path(r"{(feat_file).as_posix()}")
if FEATURE_FILE.exists():
    scenarios(str(FEATURE_FILE))

@pytest.fixture
def bdd_ctx():
    return {{"res": None}}

@when(parsers.re(r".*"))
def when_req(bdd_ctx):
    service = PbiBdd002Service()
    bdd_ctx["res"] = service.execute({{"identifier": "{clean_id}-test"}})

@then(parsers.re(r".*"))
def then_check(bdd_ctx):
    assert bdd_ctx["res"].get("success") is True
"""

    with patch("orchestrator.activities._call_gemini_for_bdd_steps", new_callable=AsyncMock) as mock_gem:
        mock_gem.return_value = mock_bdd_code
        result = await run_tdd_cycle_activity(pbi_id, component, {"design_status": "COMPLETED"})

    assert result["tdd_status"] == "ALL_GREEN"
    assert result["component"] == component
    assert Path(result["bdd_test_file"]).exists()


@pytest.mark.asyncio
async def test_integration_and_pr_activity_with_outer_bdd_loop():
    """Phase 6: 結合検証でビジネス Gherkin BDD とユニット結合テストの両方が実行され、ALL GREEN となること"""
    pbi_id = "PBI-BDD-003"
    clean_id = pbi_id.lower().replace("-", "_")

    # 1. ビジネス Feature ファイルを配置
    biz_dir = BASE_DIR / "specs" / "features" / "business"
    biz_dir.mkdir(parents=True, exist_ok=True)
    biz_file = biz_dir / f"{pbi_id}.feature"
    biz_file.write_text(
        """# language: ja
@covers:BIZ-001
機能: PBI-BDD-003 エンドツーエンドビジネス振る舞い
  シナリオ: 業務シナリオ検証
    前提 適切な前提条件が満たされている
    もし 対象の操作を実行したとき
    ならば 期待される結果が得られる
""",
        encoding="utf-8",
    )

    # 2. App および BFF のサービス実装ファイルを配置
    for comp in ("app", "bff"):
        c_dir = BASE_DIR / "src" / comp
        c_dir.mkdir(parents=True, exist_ok=True)
        impl_file = c_dir / f"{clean_id}_service.py"
        impl_file.write_text(
            f"""from typing import Dict, Any
class PbiBdd003Service:
    def execute(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        ident = payload.get("identifier", "default")
        return {{"success": True, "status_code": 200, "identifier": ident, "component": "{comp}"}}
""",
            encoding="utf-8",
        )

    app_res = {"status": "SUCCESS", "tdd_status": "ALL_GREEN", "rule_guard": {"category": "SUCCESS"}}
    bff_res = {"status": "SUCCESS", "tdd_status": "ALL_GREEN", "rule_guard": {"category": "SUCCESS"}}

    # GitHub PR 作成はモック (URL 返却)
    with patch("orchestrator.activities._create_github_pr", return_value="https://github.com/mock/pr/999"):
        result = await integration_and_pr_activity(pbi_id, app_res, bff_res)

        assert result["status"] == "COMPLETED"
        assert result["integration_status"] == "ALL_GREEN"
        assert Path(result["pr_summary_path"]).exists()

        # PR サマリーの内容を検証
        pr_content = Path(result["pr_summary_path"]).read_text(encoding="utf-8")
        assert "二重ループ TDD" in pr_content or "二重ループ" in pr_content
        assert "ビジネス Gherkin 受け入れ検証" in pr_content
        assert "重点レビュー箇所" in pr_content
