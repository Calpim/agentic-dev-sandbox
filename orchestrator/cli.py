import asyncio
import os
import sys
from typing import Optional

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

import typer
from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from temporalio.client import Client

from orchestrator.guards.validators import validate_pbi_id

app = typer.Typer(help="AI-Driven Autonomous Development CLI (Temporal Orchestrator)")
console = Console()

TASK_QUEUE = os.getenv("TEMPORAL_TASK_QUEUE", "ai-dev-task-queue")
TEMPORAL_ADDRESS = os.getenv("TEMPORAL_ADDRESS", "localhost:7233")


async def get_client() -> Client:
    try:
        return await Client.connect(TEMPORAL_ADDRESS)
    except Exception as e:
        console.print(f"[bold red]Temporal サーバーへの接続に失敗しました: {e}[/bold red]")
        console.print("[yellow]docker compose up -d を実行してサーバーを起動してください。[/yellow]")
        sys.exit(1)


@app.command()
def start(
    pbi_id: str = typer.Argument(..., help="PBIの識別子 (例: PBI-101)"),
    description: str = typer.Option("ユーザー登録およびバリデーション機能", help="PBIの概要・受け入れ条件"),
):
    """新しいPBI自律開発ワークフローを開始する"""
    try:
        pbi_id = validate_pbi_id(pbi_id)
    except ValueError as e:
        console.print(f"[bold red]入力エラー: {e}[/bold red]")
        raise typer.BadParameter(str(e))

    async def _run():
        client = await get_client()
        workflow_id = f"dev-workflow-{pbi_id}"

        await client.start_workflow(
            "PBIDevelopmentWorkflow",
            args=[pbi_id, description],
            id=workflow_id,
            task_queue=TASK_QUEUE,
        )
        console.print(
            Panel(
                f"[bold green]ワークフローを開始しました！[/bold green]\n"
                f"Workflow ID: [cyan]{workflow_id}[/cyan]\n"
                f"Web UI: [link=http://localhost:8088]http://localhost:8088[/link]\n\n"
                f"次のステップ:\n"
                f"1. `python orchestrator/cli.py status {workflow_id}` で進捗確認\n"
                f"2. Example Map を確認後、`python orchestrator/cli.py approve-example-map {workflow_id}` で承認",
                title="Workflow Started",
            )
        )

    asyncio.run(_run())


@app.command()
def status(workflow_id: str = typer.Argument(..., help="Workflow ID")):
    """現在のワークフローステータスと人間承認待ち状況を確認する"""

    async def _run():
        client = await get_client()
        handle = client.get_workflow_handle(workflow_id)

        try:
            status_data = await handle.query("get_status")
        except Exception as e:
            console.print(f"[bold red]ステータスの取得に失敗しました: {e}[/bold red]")
            return

        table = Table(title=f"Workflow Status: {workflow_id}")
        table.add_column("項目", style="cyan")
        table.add_row("PBI ID", status_data.get("pbi_id", ""))
        table.add_row("現在のステージ", f"[bold yellow]{status_data.get('stage', '')}[/bold yellow]")

        em_status = (
            "[green]承認済[/green]"
            if status_data.get("example_map_approved")
            else "[bold red]未承認（待機中）[/bold red]"
        )
        sc_status = (
            "[green]承認済[/green]" if status_data.get("schema_approved") else "[bold red]未承認（待機中）[/bold red]"
        )

        table.add_row("人間ゲート 1 (Example Map)", em_status)
        table.add_row("人間ゲート 2 (OpenAPI Schema)", sc_status)

        console.print(table)

        stage = status_data.get("stage", "")
        pbi_id = status_data.get("pbi_id", "")
        if stage == "WAITING_HUMAN_GATE_1_EXAMPLE_MAP":
            console.print(
                Panel(
                    f"[bold cyan]人間ゲート 1 待機中[/bold cyan]\n"
                    f"対象ファイル: `specs/example-maps/{pbi_id}.md`\n\n"
                    f"承認する場合: `python orchestrator/cli.py approve-example-map {workflow_id}`\n"
                    f'修正指示する場合: `python orchestrator/cli.py reject-example-map {workflow_id} --feedback "指示内容"`',
                    title="Action Required: Gate 1",
                )
            )
        elif stage == "WAITING_HUMAN_GATE_2_SCHEMA":
            console.print(
                Panel(
                    f"[bold cyan]人間ゲート 2 待機中 (OpenAPI スキーマレビュー)[/bold cyan]\n"
                    f"対象ファイル: `specs/api/v1/{pbi_id}.yaml`\n\n"
                    f"承認する場合: `python orchestrator/cli.py approve-schema {workflow_id}`\n"
                    f'修正指示する場合: `python orchestrator/cli.py reject-schema {workflow_id} --feedback "指示内容"`',
                    title="Action Required: Gate 2",
                )
            )
        elif stage == "WAITING_LIGHTWEIGHT_HIL_SCHEMA_CHANGE":
            console.print(
                Panel(
                    f"[bold yellow]軽量 HIL (P2-5): スキーマ変更要求が発生しました[/bold yellow]\n"
                    f"対象ファイル: `specs/api/v1/{pbi_id}.yaml`\n\n"
                    f"差分承認してRESYNCする場合: `python orchestrator/cli.py approve-schema-change {workflow_id}`\n"
                    f'却下/修正指示する場合: `python orchestrator/cli.py reject-schema-change {workflow_id} --feedback "指示内容"`',
                    title="Action Required: Lightweight HIL (SCHEMA_CHANGE)",
                )
            )
        elif stage == "COMPLETED":
            pr_url = status_data.get("pr_url")
            summary_path = status_data.get("pr_summary_path", f"tasks/{pbi_id}/PR_SUMMARY.md")
            pr_msg = (
                f"[bold green]GitHub PR: [link={pr_url}]{pr_url}[/link][/bold green]\n"
                if pr_url
                else "[dim]GitHub PR: 未作成（ローカルサマリーのみ）[/dim]\n"
            )
            console.print(
                Panel(
                    f"[bold green]すべてのパイプライン（要件・契約・TDD・結合検証）が完了しました！[/bold green]\n\n"
                    f"{pr_msg}"
                    f"サマリーレポート: `{summary_path}`",
                    title="Pipeline Completed",
                )
            )
        elif stage == "INTEGRATION_FAILED":
            summary_path = status_data.get("pr_summary_path", f"tasks/{pbi_id}/PR_SUMMARY.md")
            console.print(
                Panel(
                    f"[bold red]結合検証（Integration Gate）に失敗しました。[/bold red]\n"
                    f"PR の自動作成は抑止されました。\n\n"
                    f"サマリーレポート: `{summary_path}`",
                    title="Integration Gate Failed",
                )
            )
        elif stage == "PIPELINE_FAILED":
            console.print(
                Panel(
                    f"[bold red]パイプラインでエラーまたは人間エスカレーションが発生しました。[/bold red]\n"
                    f"自律ループが停止されました。\n\n"
                    f"状態確認: `tasks/{pbi_id}/state.json`\n"
                    f"ディレクティブ確認: `tasks/{pbi_id}/*/plan.md`\n"
                    f"監査ログ: `tasks/{pbi_id}/audit.jsonl`",
                    title="Pipeline Interrupted",
                )
            )

    asyncio.run(_run())


@app.command()
def approve_example_map(workflow_id: str = typer.Argument(..., help="Workflow ID")):
    """人間ゲート 1: Example Map を承認して次のフェーズへ進める"""

    async def _run():
        client = await get_client()
        handle = client.get_workflow_handle(workflow_id)
        await handle.signal("approve_example_map")
        console.print(f"[bold green][OK] Example Map を承認しました: {workflow_id}[/bold green]")
        console.print("Gherkin 生成と OpenAPI スキーマ ドラフトへ進みます。")

    asyncio.run(_run())


@app.command()
def reject_example_map(
    workflow_id: str = typer.Argument(..., help="Workflow ID"),
    feedback: str = typer.Option(..., "--feedback", "-f", help="修正指示内容"),
):
    """人間ゲート 1: Example Map の修正を指示して再生成させる"""

    async def _run():
        client = await get_client()
        handle = client.get_workflow_handle(workflow_id)
        await handle.signal("reject_example_map", args=[feedback])
        console.print(f"[bold yellow][RETRY] 修正指示を送信しました: {workflow_id}[/bold yellow]")

    asyncio.run(_run())


@app.command()
def approve_schema(workflow_id: str = typer.Argument(..., help="Workflow ID")):
    """人間ゲート 2: OpenAPI スキーマを承認して並行開発パイプラインを開始する"""

    async def _run():
        client = await get_client()
        handle = client.get_workflow_handle(workflow_id)
        await handle.signal("approve_schema")
        console.print(f"[bold green][OK] スキーマを承認しました: {workflow_id}[/bold green]")
        console.print("App / BFF の並行自律開発パイプラインが開始されます！")

    asyncio.run(_run())


@app.command()
def reject_schema(
    workflow_id: str = typer.Argument(..., help="Workflow ID"),
    feedback: str = typer.Option(..., "--feedback", "-f", help="スキーマ修正指示内容"),
):
    """人間ゲート 2: スキーマの修正を指示する"""

    async def _run():
        client = await get_client()
        handle = client.get_workflow_handle(workflow_id)
        await handle.signal("reject_schema", args=[feedback])
        console.print(f"[bold yellow][RETRY] スキーマ修正指示を送信しました: {workflow_id}[/bold yellow]")

    asyncio.run(_run())


@app.command()
def approve_schema_change(
    workflow_id: str = typer.Argument(..., help="Workflow ID"),
    feedback: Optional[str] = typer.Option(None, "--feedback", "-f", help="追加の修正指示・反映要望"),
):
    """軽量 HIL (P2-5): スキーマ変更差分を承認してパイプラインを再同期 (RESYNC) する"""

    async def _run():
        client = await get_client()
        handle = client.get_workflow_handle(workflow_id)
        await handle.signal("approve_schema_change", args=[feedback])
        console.print(f"[bold green][OK] スキーマ変更を承認しました: {workflow_id}[/bold green]")
        console.print("OpenAPI スキーマが更新され、App / BFF パイプラインが自動再同期 (RESYNC) されます！")

    asyncio.run(_run())


@app.command()
def reject_schema_change(
    workflow_id: str = typer.Argument(..., help="Workflow ID"),
    feedback: str = typer.Option(..., "--feedback", "-f", help="却下理由・差し戻し指示"),
):
    """軽量 HIL (P2-5): スキーマ変更要求を却下して差し戻す"""

    async def _run():
        client = await get_client()
        handle = client.get_workflow_handle(workflow_id)
        await handle.signal("reject_schema_change", args=[feedback])
        console.print(f"[bold red][REJECT] スキーマ変更要求を却下しました: {workflow_id}[/bold red]")

    asyncio.run(_run())


@app.command()
def run_interactive(
    pbi_id: str = typer.Argument(..., help="PBIの識別子 (例: PBI-101)"),
    description: str = typer.Option("ユーザー登録およびバリデーション機能", help="PBIの概要・受け入れ条件"),
):
    """Temporal サーバーなしで、ターミナル対話形式 (Gate 1, Gate 2, 軽量 HIL) で開発パイプラインを実行する"""
    try:
        pbi_id = validate_pbi_id(pbi_id)
    except ValueError as e:
        console.print(f"[bold red]入力エラー: {e}[/bold red]")
        raise typer.BadParameter(str(e))

    from orchestrator.interactive import InteractiveWorkflowRunner

    async def _run():
        runner = InteractiveWorkflowRunner(pbi_id=pbi_id, pbi_content=description)
        try:
            result = await runner.run()
        except KeyboardInterrupt:
            console.print("\n[yellow]ユーザー操作によりワークフローが中断されました。[/yellow]")
            return

        if result.get("status") == "COMPLETED":
            console.print(
                Panel(
                    f"[bold green]ワークフローが完了しました！[/bold green]\n"
                    f"PR URL: [cyan]{result.get('pr_url')}[/cyan]\n"
                    f"PR Summary: [cyan]{result.get('pr_summary_path')}[/cyan]",
                    title="Workflow Completed",
                )
            )
        else:
            console.print(f"[bold red]ワークフローが終了しました (Status: {result.get('status')})[/bold red]")

    asyncio.run(_run())


if __name__ == "__main__":
    app()
