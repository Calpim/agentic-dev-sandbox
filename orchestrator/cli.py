"""AI-Driven Autonomous Development CLI (v4.0 Agent-Native).

Provides command-line utilities to run interactive pipelines and inspect
task states without requiring Temporal Server or external daemons.
"""

import asyncio
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

from orchestrator.guards.validators import validate_pbi_id
from orchestrator.state_manager import StateManager

app = typer.Typer(help="AI-Driven Autonomous Development CLI (v4.0 Agent-Native)")
console = Console()
state_manager = StateManager()


@app.command()
def run_interactive(
    pbi_id: str = typer.Argument(..., help="PBIの識別子 (例: PBI-101)"),
    description: str = typer.Option("ユーザー登録およびバリデーション機能", help="PBIの概要・受け入れ条件"),
):
    """ターミナル対話形式 (Gate 1, Gate 2, 軽量 HIL) で開発パイプラインを実行する"""
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


@app.command()
def init(
    pbi_id: str = typer.Argument(..., help="PBIの識別子 (例: PBI-101)"),
    schema_path: Optional[str] = typer.Option(None, "--schema", "-s", help="OpenAPI スキーマファイルのパス"),
):
    """PBI のタスク状態 (tasks/{pbi_id}/state.json, plan.md) を初期化する"""
    try:
        pbi_id = validate_pbi_id(pbi_id)
    except ValueError as e:
        console.print(f"[bold red]入力エラー: {e}[/bold red]")
        raise typer.BadParameter(str(e))

    task_dir = state_manager.init_pbi(pbi_id, schema_path=schema_path)
    console.print(
        Panel(
            f"[bold green]PBI タスクを初期化しました: {pbi_id}[/bold green]\n"
            f"Task Directory: [cyan]{task_dir}[/cyan]\n"
            f"State File: [cyan]{task_dir / 'state.json'}[/cyan]",
            title="Task Initialized",
        )
    )


@app.command()
def status(
    pbi_id: str = typer.Argument(..., help="PBIの識別子 (例: PBI-101)"),
):
    """PBI の現在のパイプライン進捗 (state.json) を確認する"""
    try:
        pbi_id = validate_pbi_id(pbi_id)
    except ValueError as e:
        console.print(f"[bold red]入力エラー: {e}[/bold red]")
        raise typer.BadParameter(str(e))

    data = state_manager.get_state(pbi_id)
    if not data:
        console.print(f"[bold red]タスク情報が見つかりません: {pbi_id}[/bold red]")
        sys.exit(1)

    table = Table(title=f"Task Status: {pbi_id}")
    table.add_column("Component", style="cyan")
    table.add_column("Phase", style="yellow")
    table.add_column("Status", style="green")

    pipelines = data.get("pipelines", {})
    for comp, info in pipelines.items():
        table.add_row(
            comp.upper(),
            info.get("phase", "UNKNOWN"),
            info.get("status", "UNKNOWN"),
        )

    console.print(table)


@app.command()
def directive(
    pbi_id: str = typer.Argument(..., help="PBIの識別子 (例: PBI-101)"),
    component: str = typer.Argument(..., help="対象コンポーネント (app または bff)"),
    action: str = typer.Argument(..., help="ディレクティブ (例: FIX_TEST, RETRY_IMPL, RETRY_DESIGN, RESYNC)"),
    reason: str = typer.Option("", "--reason", "-r", help="ディレクティブ理由"),
    instruction: str = typer.Option("", "--instruction", "-i", help="具体的な指示内容"),
):
    """コンポーネントの plan.md の Current Directive を更新する"""
    try:
        pbi_id = validate_pbi_id(pbi_id)
    except ValueError as e:
        console.print(f"[bold red]入力エラー: {e}[/bold red]")
        raise typer.BadParameter(str(e))

    if component.lower() not in ("app", "bff"):
        console.print(f"[bold red]無効なコンポーネントです: {component} (app または bff を指定してください)[/bold red]")
        sys.exit(1)

    state_manager.set_directive(
        pbi_id=pbi_id,
        component=component.lower(),
        directive=action,
        reason=reason,
        instruction=instruction,
    )
    console.print(f"[bold green]Directive を更新しました: {pbi_id} ({component}) -> {action}[/bold green]")


if __name__ == "__main__":
    app()
