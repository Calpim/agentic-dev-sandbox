import json
import os
import shutil
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from orchestrator.activities import (
    BASE_DIR,
    _build_pr_body,
    run_analyzer_activity,
)
from orchestrator.guards.analyzer import (
    DirectiveType,
    analyze_failure,
    classify_failure_heuristics,
)
from orchestrator.workflow import ComponentPipelineWorkflow


@pytest.fixture(autouse=True)
def cleanup_test_artifacts():
    """テスト実行前後にテスト用PBIディレクトリをクリーンアップ"""
    test_pbis = ["PBI-TEST-901", "PBI-TEST-902", "PBI-TEST-903"]
    for pbi in test_pbis:
        pdir = BASE_DIR / "tasks" / pbi
        if pdir.exists():
            shutil.rmtree(pdir, ignore_errors=True)
    yield
    for pbi in test_pbis:
        pdir = BASE_DIR / "tasks" / pbi
        if pdir.exists():
            shutil.rmtree(pdir, ignore_errors=True)


def test_heuristic_classification_circuit_breaker():
    """サーキットブレーカー: サイクル回数が上限を超過した場合に ESCALATE_HUMAN となること"""
    directive = classify_failure_heuristics(
        test_output="AssertionError: assert False",
        cycle_count=3,
        max_cycles=2,
    )
    assert directive.directive == DirectiveType.ESCALATE_HUMAN
    assert directive.cycle_count == 3
    assert "上限 (2回) を超過しました" in directive.reason

    # 上限内 (cycle_count == 2 <= max_cycles) の場合は自己修復分析が継続されること
    d_within = classify_failure_heuristics(
        test_output="KeyError: 'foo'",
        cycle_count=2,
        max_cycles=2,
    )
    assert d_within.directive == DirectiveType.RETRY_IMPL


def test_heuristic_classification_directives():
    """各キーワードに応じたヒューリスティック分類の検証"""
    # 1. スキーマ契約不備 -> SCHEMA_CHANGE
    d_schema = classify_failure_heuristics(
        test_output="Schema validation error: field 'total' is missing",
        cycle_count=1,
        max_cycles=2,
        test_file_path="src/app/tests/test_foo.py",
    )
    assert d_schema.directive == DirectiveType.SCHEMA_CHANGE
    assert "specs/api/v1/foo.yaml" in d_schema.target_files[0]

    # 2. テスト自体の不備 -> FIX_TEST
    d_test = classify_failure_heuristics(
        test_output="Test defect: assertion on wrong expectation in test_assert_bug",
        cycle_count=1,
        max_cycles=2,
        test_file_path="src/app/tests/test_foo.py",
    )
    assert d_test.directive == DirectiveType.FIX_TEST
    assert d_test.target_files == ["src/app/tests/test_foo.py"]

    # フィクスチャ欠落エラーも FIX_TEST として検知されること
    d_fixture = classify_failure_heuristics(
        test_output="fixture 'auth_client' not found",
        cycle_count=1,
        max_cycles=2,
        test_file_path="src/app/tests/test_foo.py",
    )
    assert d_fixture.directive == DirectiveType.FIX_TEST

    # 3. アーキテクチャ不整合 -> RETRY_DESIGN
    d_design = classify_failure_heuristics(
        test_output="Architecture violation: missing interface contract",
        cycle_count=1,
        max_cycles=2,
    )
    assert d_design.directive == DirectiveType.RETRY_DESIGN

    # 4. 実装ロジックエラー -> RETRY_IMPL
    d_impl = classify_failure_heuristics(
        test_output="KeyError: 'user_id' not found in result",
        cycle_count=1,
        max_cycles=2,
        impl_file_path="src/app/foo_service.py",
    )
    assert d_impl.directive == DirectiveType.RETRY_IMPL
    assert d_impl.target_files == ["src/app/foo_service.py"]


@pytest.mark.asyncio
async def test_analyze_failure_llm_success():
    """LLM による推論が成功し、構造化 Directive が返却されること"""
    mock_llm_json = json.dumps(
        {
            "directive": "FIX_TEST",
            "reason": "テストコードのアサーションが Example Map の受入条件と乖離しているため",
            "instruction": "test_login の期待値を 200 に修正してください",
            "target_files": ["src/app/tests/test_login.py"],
            "confidence": 0.95,
        }
    )

    mock_client = MagicMock()
    mock_response = MagicMock()
    mock_response.text = mock_llm_json
    mock_client.aio.models.generate_content = AsyncMock(return_value=mock_response)

    with patch.dict(os.environ, {"GEMINI_API_KEY": "fake_key"}):
        with patch("google.genai.Client", return_value=mock_client):
            directive = await analyze_failure(
                pbi_id="PBI-TEST-901",
                component="app",
                test_output="FAILED test_login: assert 401 == 200",
                cycle_count=1,
                max_cycles=2,
            )

            assert directive.directive == DirectiveType.FIX_TEST
            assert directive.confidence == 0.95
            assert directive.target_files == ["src/app/tests/test_login.py"]
            assert directive.cycle_count == 1


@pytest.mark.asyncio
async def test_analyze_failure_fallback_on_error():
    """LLM 呼び出しが失敗した際、ヒューリスティックに自動フォールバックすること"""
    mock_client = MagicMock()
    mock_client.aio.models.generate_content = AsyncMock(side_effect=RuntimeError("API quota exceeded"))

    with patch.dict(os.environ, {"GEMINI_API_KEY": "fake_key"}):
        with patch("google.genai.Client", return_value=mock_client):
            directive = await analyze_failure(
                pbi_id="PBI-TEST-901",
                component="bff",
                test_output="Test defect: assertion error in test_assert_bug",
                cycle_count=1,
                max_cycles=2,
                test_file_path="src/bff/tests/test_foo.py",
            )

            assert directive.directive == DirectiveType.FIX_TEST
            assert "テストコード自体のアサーション" in directive.reason


@pytest.mark.asyncio
async def test_run_analyzer_activity_updates_state_and_plan():
    """run_analyzer_activity が state.json、plan.md、audit.jsonl を適切に更新すること"""
    pbi_id = "PBI-TEST-901"
    component = "app"
    task_dir = BASE_DIR / "tasks" / pbi_id
    comp_dir = task_dir / component
    comp_dir.mkdir(parents=True, exist_ok=True)

    state_file = task_dir / "state.json"
    state_file.write_text(
        json.dumps(
            {
                "pbi_id": pbi_id,
                "pipelines": {
                    component: {
                        "analyzer_cycles": 0,
                        "status": "IN_PROGRESS",
                    }
                },
            }
        ),
        encoding="utf-8",
    )

    plan_file = comp_dir / "plan.md"
    plan_file.write_text("# Initial Plan\n- Step 1: Design\n", encoding="utf-8")

    failure_context = {
        "verify_output": "AssertionError: Expected 200 but got 500",
        "error": "Pytest failed",
    }

    # API キーを無効化してヒューリスティック分類を実行
    with patch.dict(os.environ, {"GEMINI_API_KEY": "", "GOOGLE_API_KEY": ""}):
        result = await run_analyzer_activity(pbi_id, component, failure_context)

    assert result["status"] == "COMPLETED"
    directive_data = result["directive"]
    assert directive_data["directive"] == DirectiveType.RETRY_IMPL.value

    # 1. state.json 検証
    st_data = json.loads(state_file.read_text(encoding="utf-8"))
    assert st_data["pipelines"][component]["analyzer_cycles"] == 1
    assert st_data["pipelines"][component]["last_directive"]["directive"] == "RETRY_IMPL"

    # 2. plan.md 検証
    plan_text = plan_file.read_text(encoding="utf-8")
    assert "## 🚨 Analyzer Directive: RETRY_IMPL" in plan_text
    assert "Cycle 1/2" in plan_text

    # 3. audit.jsonl 検証
    audit_file = task_dir / "audit.jsonl"
    assert audit_file.exists()
    audit_lines = [json.loads(line) for line in audit_file.read_text(encoding="utf-8").splitlines() if line.strip()]
    assert len(audit_lines) == 1
    assert audit_lines[0]["event"] == "ANALYZER_DIRECTIVE"
    assert audit_lines[0]["component"] == component
    assert audit_lines[0]["directive"]["directive"] == "RETRY_IMPL"


def test_pr_body_includes_analyzer_history():
    """audit.jsonl の Analyzer 介入ログが PR_SUMMARY 本文にレンダリングされること"""
    pbi_id = "PBI-TEST-902"
    task_dir = BASE_DIR / "tasks" / pbi_id
    task_dir.mkdir(parents=True, exist_ok=True)

    audit_file = task_dir / "audit.jsonl"
    audit_file.write_text(
        json.dumps(
            {
                "event": "ANALYZER_DIRECTIVE",
                "pbi_id": pbi_id,
                "component": "app",
                "cycle": 1,
                "directive": {
                    "directive": "FIX_TEST",
                    "reason": "受入条件との乖離によるテストアサーション修正",
                },
            }
        )
        + "\n",
        encoding="utf-8",
    )

    app_res = {"status": "SUCCESS", "tdd_status": "ALL_GREEN", "rule_guard": {"category": "SUCCESS"}}
    bff_res = {"status": "SUCCESS", "tdd_status": "ALL_GREEN", "rule_guard": {"category": "SUCCESS"}}
    integration_res = {"status": "ALL_GREEN", "rule_guard": {"category": "SUCCESS"}}

    body = _build_pr_body(pbi_id, app_res, bff_res, integration_res)

    assert "## 🤖 Analyzer 介入実績 (Phase 5-3 自律自己修復)" in body
    assert "**[APP] Cycle 1 (FIX_TEST)**: 受入条件との乖離によるテストアサーション修正" in body
    assert "重点レビュー箇所" in body
    assert "Analyzer による自己修復 (FIX_TEST / RETRY_IMPL)" in body


@pytest.mark.asyncio
async def test_component_pipeline_workflow_autonomous_loop():
    """ComponentPipelineWorkflow が TDD 失敗時に Analyzer を経由して自己修復できること"""
    pbi_id = "PBI-TEST-903"
    component = "app"

    # 1 回目 TDD: 失敗 (VERIFY_FAILED)
    # 2 回目 TDD: 成功 (ALL_GREEN)
    tdd_call_count = 0

    async def mock_execute_activity(activity_name, args, **kwargs):
        nonlocal tdd_call_count
        if activity_name == "run_design_activity":
            return {"design_status": "COMPLETED"}
        elif activity_name == "run_tdd_cycle_activity":
            tdd_call_count += 1
            if tdd_call_count == 1:
                return {
                    "tdd_status": "VERIFY_FAILED",
                    "error": "Test defect: wrong assert",
                }
            else:
                return {
                    "tdd_status": "ALL_GREEN",
                }
        elif activity_name == "run_analyzer_activity":
            return {
                "directive": {
                    "directive": "FIX_TEST",
                    "reason": "テストのアサーション修正が必要",
                    "instruction": "assert 条件を仕様に合わせてください",
                }
            }
        return {}

    wf = ComponentPipelineWorkflow()
    with patch("temporalio.workflow.execute_activity", side_effect=mock_execute_activity):
        result = await wf.run(pbi_id, component)

        assert result["status"] == "SUCCESS"
        assert tdd_call_count == 2
        assert result["details"]["tdd_status"] == "ALL_GREEN"


@pytest.mark.asyncio
async def test_component_pipeline_workflow_escalates_to_human():
    """サーキットブレーカー等で ESCALATE_HUMAN が返された場合、パイプラインが中断すること"""
    pbi_id = "PBI-TEST-903"
    component = "bff"

    async def mock_execute_activity(activity_name, args, **kwargs):
        if activity_name == "run_design_activity":
            return {"design_status": "COMPLETED"}
        elif activity_name == "run_tdd_cycle_activity":
            return {"tdd_status": "VERIFY_FAILED", "error": "Persistent error"}
        elif activity_name == "run_analyzer_activity":
            return {
                "directive": {
                    "directive": "ESCALATE_HUMAN",
                    "reason": "サイクル上限到達",
                    "instruction": "人間によるレビューが必要です",
                }
            }
        return {}

    wf = ComponentPipelineWorkflow()
    with patch("temporalio.workflow.execute_activity", side_effect=mock_execute_activity):
        result = await wf.run(pbi_id, component)

        assert result["status"] == "ESCALATED_HUMAN"
        assert result["directive"]["directive"] == "ESCALATE_HUMAN"
