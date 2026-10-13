"""AI-Driven Autonomous Development CLI (v4.0 Agent-Native).

Provides command-line utilities to inspect and manage task states without
requiring Temporal Server or external daemons.
"""

import sys
from pathlib import Path
from typing import Optional

import typer
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

# Ensure repository root is on sys.path when invoked directly as a script
repo_root = Path(__file__).resolve().parent.parent
if str(repo_root) not in sys.path:
    sys.path.insert(0, str(repo_root))

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

from orchestrator.guards.validators import validate_pbi_id  # noqa: E402
from orchestrator.state_manager import StateManager  # noqa: E402

app = typer.Typer(help="AI-Driven Autonomous Development CLI (v4.0 Agent-Native)")
console = Console()
state_manager = StateManager()


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

    try:
        task_dir = state_manager.init_pbi(pbi_id, schema_path=schema_path)
    except FileExistsError as e:
        console.print(f"[bold yellow]タスクは初期化済みです。既存状態を保持しました: {pbi_id}[/bold yellow]")
        raise typer.BadParameter(str(e))
    console.print(
        Panel(
            f"[bold green]PBI タスクを初期化しました: {pbi_id}[/bold green]\n"
            f"Task Directory: [cyan]{task_dir}[/cyan]\n"
            f"State File: [cyan]{task_dir / 'state.json'}[/cyan]",
            title="Task Initialized",
        )
    )


@app.command()
def reset(
    pbi_id: str = typer.Argument(..., help="PBIの識別子 (例: PBI-101)"),
    schema_path: Optional[str] = typer.Option(None, "--schema", "-s", help="OpenAPI スキーマファイルのパス"),
):
    """state.json と plan.md を初期化する。audit/spec/実装ファイルは保持する。"""
    try:
        pbi_id = validate_pbi_id(pbi_id)
    except ValueError as e:
        console.print(f"[bold red]入力エラー: {e}[/bold red]")
        raise typer.BadParameter(str(e))

    task_dir = state_manager.reset_pbi(pbi_id, schema_path=schema_path)
    console.print(
        Panel(
            f"[bold yellow]タスク状態と plan をリセットしました: {pbi_id}[/bold yellow]\n"
            "監査ログ、仕様ファイル、実装ファイルは保持しています。\n"
            f"Task Directory: [cyan]{task_dir}[/cyan]",
            title="Task Reset",
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
def phase(
    pbi_id: str = typer.Argument(..., help="PBIの識別子 (例: PBI-101)"),
    component: str = typer.Argument(..., help="対象コンポーネント (app または bff)"),
    phase_name: str = typer.Argument(..., help="フェーズ名 (例: DESIGN, RED, GREEN, REFACTOR, DONE)"),
    status: str = typer.Option("IN_PROGRESS", "--status", help="ステータス (例: IN_PROGRESS, COMPLETED)"),
):
    """コンポーネント (app / bff) の開発フェーズ (state.json) を更新する"""
    try:
        pbi_id = validate_pbi_id(pbi_id)
    except ValueError as e:
        console.print(f"[bold red]入力エラー: {e}[/bold red]")
        raise typer.BadParameter(str(e))

    comp_norm = component.lower()
    if comp_norm not in ("app", "bff"):
        console.print(f"[bold red]無効なコンポーネントです: {component} (app または bff を指定してください)[/bold red]")
        sys.exit(1)

    try:
        state_manager.set_component_phase(
            pbi_id=pbi_id,
            component=comp_norm,
            phase=phase_name,
            status=status,
        )
    except ValueError as e:
        console.print(f"[bold red]入力エラー: {e}[/bold red]")
        raise typer.BadParameter(str(e))
    console.print(
        f"[bold green]フェーズを更新しました: {pbi_id} ({comp_norm}) -> {phase_name.upper()} ({status})[/bold green]"
    )


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
