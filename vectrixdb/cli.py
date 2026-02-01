"""
VectrixDB CLI - Command line interface.

Usage:
    vectrixdb serve --port 7337
    vectrixdb info ./my_vectors
    vectrixdb collections list ./my_vectors

Author: Daddy Nyame Owusu - Boakye
"""

import typer
from rich.console import Console
from rich.table import Table
from rich.panel import Panel
from typing import Optional

app = typer.Typer(
    name="vectrixdb",
    help="VectrixDB - Where vectors come alive",
    add_completion=False,
)
console = Console()


BANNER = """
 _    _        _       _      ___  ___
| |  | |      | |     (_)     |  \\/  |
| |  | |  ___ | |  __  _  __  | .  . |
| |/\\| | / _ \\| | / / | ||__| | |\\/| |
\\  /\\  /|  __/| |/ /  | | __  | |  | |
 \\/  \\/  \\___||___/   |_||__| \\_|  |_/

 VectrixDB - Where vectors come alive
"""


@app.command()
def serve(
    port: int = typer.Option(7337, "--port", "-p", help="Port to run on"),
    host: str = typer.Option("0.0.0.0", "--host", "-h", help="Host to bind to"),
    path: str = typer.Option("./vectrixdb_data", "--path", "-d", help="Database path"),
    reload: bool = typer.Option(False, "--reload", "-r", help="Enable auto-reload"),
    dashboard: bool = typer.Option(True, "--dashboard/--no-dashboard", help="Enable dashboard"),
    api_key: Optional[str] = typer.Option(None, "--api-key", "-k", help="API key for authentication"),
    read_only_key: Optional[str] = typer.Option(None, "--read-only-key", help="Read-only API key"),
):
    """Start the VectrixDB server."""
    console.print(Panel(BANNER, style="cyan", border_style="cyan"))
    console.print(f"[bold green]Starting VectrixDB server...[/bold green]")
    console.print(f"  [dim]Database:[/dim] {path}")
    console.print(f"  [dim]Server:[/dim] http://{host}:{port}")
    console.print(f"  [dim]Dashboard:[/dim] http://{host}:{port}/dashboard")
    console.print(f"  [dim]API Docs:[/dim] http://{host}:{port}/docs")
    if api_key:
        console.print(f"  [dim]API Key:[/dim] {api_key[:8]}..." if len(api_key) > 8 else f"  [dim]API Key:[/dim] {api_key}")
    else:
        console.print(f"  [dim]API Key:[/dim] disabled")
    console.print()

    from .api.server import run_server

    run_server(host=host, port=port, db_path=path, reload=reload, api_key=api_key, read_only_key=read_only_key)


@app.command()
def info(
    path: str = typer.Argument("./vectrixdb_data", help="Database path"),
):
    """Show database information."""
    from .core.database import VectrixDB

    try:
        db = VectrixDB(path)
        info = db.info()

        console.print(Panel(BANNER, style="cyan", border_style="cyan"))

        table = Table(title="Database Info", show_header=False)
        table.add_column("Property", style="cyan")
        table.add_column("Value", style="green")

        table.add_row("Path", info.path)
        table.add_row("Version", info.version)
        table.add_row("Collections", str(info.collections_count))
        table.add_row("Total Vectors", f"{info.total_vectors:,}")
        table.add_row("Total Size", _format_size(info.total_size_bytes))
        table.add_row("Created", info.created_at.strftime("%Y-%m-%d %H:%M:%S"))

        console.print(table)
        db.close()

    except Exception as e:
        console.print(f"[red]Error:[/red] {e}")
        raise typer.Exit(1)


@app.command("list")
def list_collections(
    path: str = typer.Argument("./vectrixdb_data", help="Database path"),
):
    """List all collections."""
    from .core.database import VectrixDB

    try:
        db = VectrixDB(path)
        collections = db.list_collections()

        if not collections:
            console.print("[yellow]No collections found.[/yellow]")
            return

        table = Table(title="Collections")
        table.add_column("Name", style="cyan")
        table.add_column("Dimension", justify="right")
        table.add_column("Metric", style="magenta")
        table.add_column("Vectors", justify="right", style="green")
        table.add_column("Size", justify="right")

        for c in collections:
            table.add_row(
                c.name,
                str(c.dimension),
                c.metric.value,
                f"{c.count:,}",
                _format_size(c.size_bytes),
            )

        console.print(table)
        db.close()

    except Exception as e:
        console.print(f"[red]Error:[/red] {e}")
        raise typer.Exit(1)


@app.command()
def create(
    name: str = typer.Argument(..., help="Collection name"),
    dimension: int = typer.Argument(..., help="Vector dimension"),
    path: str = typer.Option("./vectrixdb_data", "--path", "-d", help="Database path"),
    metric: str = typer.Option("cosine", "--metric", "-m", help="Distance metric"),
):
    """Create a new collection."""
    from .core.database import VectrixDB
    from .core.types import DistanceMetric

    try:
        db = VectrixDB(path)
        collection = db.create_collection(
            name=name,
            dimension=dimension,
            metric=DistanceMetric(metric),
        )
        console.print(f"[green]Created collection:[/green] {name}")
        console.print(f"  [dim]Dimension:[/dim] {dimension}")
        console.print(f"  [dim]Metric:[/dim] {metric}")
        db.close()

    except ValueError as e:
        console.print(f"[red]Error:[/red] {e}")
        raise typer.Exit(1)


@app.command()
def delete(
    name: str = typer.Argument(..., help="Collection name"),
    path: str = typer.Option("./vectrixdb_data", "--path", "-d", help="Database path"),
    force: bool = typer.Option(False, "--force", "-f", help="Skip confirmation"),
):
    """Delete a collection."""
    from .core.database import VectrixDB

    if not force:
        confirm = typer.confirm(f"Delete collection '{name}'?")
        if not confirm:
            raise typer.Abort()

    try:
        db = VectrixDB(path)
        if db.delete_collection(name):
            console.print(f"[green]Deleted collection:[/green] {name}")
        else:
            console.print(f"[yellow]Collection not found:[/yellow] {name}")
        db.close()

    except Exception as e:
        console.print(f"[red]Error:[/red] {e}")
        raise typer.Exit(1)


@app.command()
def version():
    """Show version information."""
    from . import __version__

    console.print(Panel(BANNER, style="cyan", border_style="cyan"))
    console.print(f"[bold]VectrixDB[/bold] v{__version__}")
    console.print("[dim]Where vectors come alive[/dim]")


@app.command("download-models")
def download_models(
    model_type: str = typer.Option(
        "all",
        "--type", "-t",
        help="Model type: all, dense, sparse, or reranker"
    ),
    force: bool = typer.Option(
        False,
        "--force", "-f",
        help="Re-download even if models exist"
    ),
):
    """
    Download embedding models (one-time setup).

    After running this command, VectrixDB works completely offline
    with no network calls.

    Models downloaded:
    - dense: all-MiniLM-L6-v2 (~23MB) - semantic embeddings
    - sparse: BM25 vocabulary (~1MB) - keyword search
    - reranker: ms-marco-MiniLM (~80MB) - result reranking

    Example:
        vectrixdb download-models          # Download all
        vectrixdb download-models -t dense # Just dense model
    """
    console.print(Panel(BANNER, style="cyan", border_style="cyan"))
    console.print("[bold green]VectrixDB Model Setup[/bold green]")
    console.print()
    console.print("[dim]This is a one-time setup. After download, VectrixDB[/dim]")
    console.print("[dim]works completely offline with no network calls.[/dim]")
    console.print()

    try:
        from .models import download_models as do_download, is_models_installed, get_models_dir

        # Check what's already installed
        if not force:
            if model_type == "all":
                if is_models_installed("all"):
                    console.print("[green]All models already installed![/green]")
                    console.print(f"[dim]Location: {get_models_dir()}[/dim]")
                    console.print("[dim]Use --force to re-download.[/dim]")
                    return
            else:
                if is_models_installed(model_type):
                    console.print(f"[green]Model '{model_type}' already installed![/green]")
                    console.print("[dim]Use --force to re-download.[/dim]")
                    return

        # Download models
        console.print(f"[yellow]Downloading {model_type} model(s)...[/yellow]")
        console.print()

        do_download(model_type=model_type, force=force, progress=True)

        console.print()
        console.print("[bold green]Setup complete![/bold green]")
        console.print(f"[dim]Models saved to: {get_models_dir()}[/dim]")
        console.print()
        console.print("[dim]You can now use VectrixDB completely offline:[/dim]")
        console.print()
        console.print("  [cyan]from vectrixdb import V[/cyan]")
        console.print("  [cyan]db = V('docs').add(['hello world'])[/cyan]")
        console.print("  [cyan]results = db.search('greeting')[/cyan]")

    except ImportError as e:
        console.print(f"[red]Missing dependencies for model download.[/red]")
        console.print()
        console.print("[yellow]Install with:[/yellow]")
        console.print("  pip install vectrixdb[setup-models]")
        console.print()
        console.print("[dim]This installs torch, transformers, and optimum[/dim]")
        console.print("[dim]for converting models to ONNX format.[/dim]")
        raise typer.Exit(1)

    except Exception as e:
        console.print(f"[red]Error:[/red] {e}")
        raise typer.Exit(1)


@app.command("models-info")
def models_info():
    """Show information about installed models."""
    console.print(Panel(BANNER, style="cyan", border_style="cyan"))

    try:
        from .models import is_models_installed, get_models_dir, MODEL_CONFIG

        models_dir = get_models_dir()

        table = Table(title="Installed Models")
        table.add_column("Model", style="cyan")
        table.add_column("Type", style="magenta")
        table.add_column("Size", justify="right")
        table.add_column("Status", style="green")

        for model_type, config in MODEL_CONFIG.items():
            installed = is_models_installed(model_type)
            status = "[green]Installed[/green]" if installed else "[red]Not installed[/red]"
            size = f"~{config.get('size_mb', '?')}MB"

            table.add_row(
                config.get("name", model_type),
                model_type,
                size,
                status,
            )

        console.print(table)
        console.print()
        console.print(f"[dim]Models directory: {models_dir}[/dim]")

        if not is_models_installed("all"):
            console.print()
            console.print("[yellow]Run 'vectrixdb download-models' to install missing models.[/yellow]")

    except Exception as e:
        console.print(f"[red]Error:[/red] {e}")
        raise typer.Exit(1)


def _format_size(bytes: int) -> str:
    """Format bytes as human readable."""
    for unit in ["B", "KB", "MB", "GB", "TB"]:
        if bytes < 1024:
            return f"{bytes:.1f} {unit}"
        bytes /= 1024
    return f"{bytes:.1f} PB"


if __name__ == "__main__":
    app()
