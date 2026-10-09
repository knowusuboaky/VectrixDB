"""
VectrixDB CLI - Command line interface.

Usage:
    vectrixdb serve --port 7337
    vectrixdb info ./my_vectors
    vectrixdb collections list ./my_vectors

Author: Kwadwo Daddy Nyame Owusu - Boakye
"""

import typer
from rich.console import Console
from rich.table import Table
from rich.panel import Panel
from typing import Any, List, Optional, cast


__all__ = [
    "app",
]


# ============================================================================
# SETTINGS: the app, the console, the banner, and the shared help
# ============================================================================
#
# The Typer app and its console, the banner serve prints, and the help every
# command shares for --path and --env-file.

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


_PATH_HELP = "Database path. Default: VECTRIXDB_PATH, or ./vectrixdb_data"
_ENV_FILE_HELP = "Read settings from this file first. What the environment already sets wins"


# ============================================================================
# THE SETTINGS A COMMAND READS
# ============================================================================
#
# INPUT   --path and --env-file
# OUTPUT  the env file read into the environment, the environment winning; the
#         data path, --path, else VECTRIXDB_PATH, else ./vectrixdb_data
#
# Every command reads its settings the way the server does, so what a command
# sees is what a start would.


def _env_file(env_file: Optional[str]) -> None:
    """Read an env file into the environment before anything reads a setting. The environment wins."""
    if not env_file:
        return
    from .exceptions import ConfigurationError
    from .settings import apply_env_file

    try:
        apply_env_file(env_file)
    except ConfigurationError as exc:
        console.print(f"[red]{exc}[/red]", markup=True, highlight=False)
        raise typer.Exit(code=2)


def _data_path(path: Optional[str]) -> str:
    """--path, else VECTRIXDB_PATH, else ./vectrixdb_data."""
    import os as _os

    return path or _os.environ.get("VECTRIXDB_PATH", "").strip() or "./vectrixdb_data"


def _settings(path: Optional[str], env_file: Optional[str]) -> str:
    """Read the env file, then say where the data is: where the server started with the same settings looks."""
    _env_file(env_file)
    return _data_path(path)


# ============================================================================
# SERVE, INFO, COLLECTIONS, INGEST, QUERY, AND STATS
# ============================================================================
#
# INPUT   the command line
# OUTPUT  the server started; the database's information; collections listed,
#         created and deleted; files loaded, chunked and indexed; a collection
#         searched from the shell; a collection's size, model and mode
#
# The everyday commands, each a few options over the library's own calls.


@app.command()
def serve(
    port: int = typer.Option(
        7337,
        "--port",
        "-p",
        envvar="VECTRIXDB_LISTEN_PORT",
        help="Port to run on. Default: VECTRIXDB_LISTEN_PORT, else 7337",
    ),
    host: str = typer.Option(
        "127.0.0.1", "--host", "-h", help="Host to bind to. 0.0.0.0 needs an API key or sign-in"
    ),
    path: Optional[str] = typer.Option(None, "--path", "-d", help=_PATH_HELP),
    reload: bool = typer.Option(False, "--reload", "-r", help="Enable auto-reload"),
    dashboard: bool = typer.Option(True, "--dashboard/--no-dashboard", help="Enable dashboard"),
    api_key: Optional[str] = typer.Option(
        None, "--api-key", "-k", help="API key for authentication"
    ),
    read_only_key: Optional[str] = typer.Option(None, "--read-only-key", help="Read-only API key"),
    env_file: Optional[str] = typer.Option(None, "--env-file", help=_ENV_FILE_HELP),
):
    """Start the VectrixDB server."""
    _env_file(env_file)
    path = _data_path(path)
    console.print(Panel(BANNER, style="cyan", border_style="cyan"))
    console.print(f"[bold green]Starting VectrixDB server...[/bold green]")
    console.print(f"  [dim]Database:[/dim] {path}")
    console.print(f"  [dim]Server:[/dim] http://{host}:{port}")
    if dashboard:
        console.print(f"  [dim]Dashboard:[/dim] http://{host}:{port}/dashboard")
    else:
        console.print("  [dim]Dashboard:[/dim] disabled")
    console.print(f"  [dim]API Docs:[/dim] http://{host}:{port}/docs")
    # Said to be set, never shown: a terminal is scrolled back, shared and recorded.
    console.print(f"  [dim]API Key:[/dim] {'set' if api_key else 'not set'}")
    import os as _os

    from rich.markup import escape

    from .exceptions import ConfigurationError

    methods = _os.environ.get("VECTRIXDB_SIGNIN", "").strip()
    if methods:
        console.print(f"  [dim]Sign-in:[/dim] {escape(methods)}")
    no_admin = False
    try:
        if methods:
            from .signin import SignInConfig
            from .signin.records import describe_where

            signin = SignInConfig.from_env(path)
            console.print(
                f"  [dim]Sign-in store:[/dim] {escape(describe_where(signin.store_url or signin.store_path))}"
            )
        from .brand import Brand

        brand = Brand.from_env().banner()
        if brand:
            console.print(f"  [dim]Brand:[/dim] {escape(brand)}")
        if "email" in methods.lower():
            config, store = _signin_store(path)
            try:
                # An admin named in VECTRIXDB_SIGNIN_USERS and not here yet is added as the server starts.
                coming = any(
                    role == "admin" and store.person(email) is None for email, role in config.users
                )
                no_admin = not store.admins() and not coming
            finally:
                store.close()
    except ConfigurationError as exc:
        console.print(f"[red]{escape(str(exc))}[/red]")
        raise typer.Exit(code=2)
    if no_admin:
        # A command to run, not a link: the start-up banner never prints anything that opens a door.
        console.print()
        console.print(
            "[yellow]No admin yet. To add the first one, run this on the server:[/yellow]"
        )
        console.print("  vectrixdb people add you@company.com --role admin", markup=False)
    console.print()

    from .api.server import run_server

    try:
        run_server(
            host=host,
            port=port,
            db_path=path,
            reload=reload,
            api_key=api_key,
            read_only_key=read_only_key,
            enable_dashboard=dashboard,
        )
    except ConfigurationError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(code=2)


@app.command("extract-serve")
def extract_serve(
    port: int = typer.Option(
        7338,
        "--port",
        "-p",
        envvar="VECTRIXDB_EXTRACT_LISTEN_PORT",
        help="Port to run on. Default: VECTRIXDB_EXTRACT_LISTEN_PORT, else 7338",
    ),
    host: str = typer.Option("127.0.0.1", "--host", "-h", help="Host to bind to"),
    prefix: Optional[str] = typer.Option(
        None,
        "--prefix",
        help="The path every route lives under, such as /api. Default: VECTRIXDB_EXTRACT_PREFIX, else the root",
    ),
    env_file: Optional[str] = typer.Option(None, "--env-file", help=_ENV_FILE_HELP),
):
    """Start the extraction service: files, addresses and videos in, text out.

    The app create_extraction_app builds, served on its own. It reads its
    services from the environment, Azure Document Intelligence and Speech or
    the readers on this machine, and will not start without VECTRIXDB_API_KEY
    or sign-in, because a call can spend money on a paid service.
    """
    _env_file(env_file)
    from rich.markup import escape

    from .exceptions import ConfigurationError

    console.print(Panel(BANNER, style="cyan", border_style="cyan"))
    console.print("[bold green]Starting the VectrixDB extraction service...[/bold green]")
    try:
        import uvicorn

        from .api.extraction import create_extraction_app, route_prefix
    except ImportError as exc:
        # The service is FastAPI and uvicorn, which only the api extra installs.
        console.print(f"[red]{escape(str(exc))}[/red]")
        console.print(r"Install it with: pip install vectrixdb\[api,documents]")
        raise typer.Exit(code=2)
    try:
        application = create_extraction_app(prefix=prefix)
    except ConfigurationError as exc:
        console.print(f"[red]{escape(str(exc))}[/red]")
        raise typer.Exit(code=2)
    import os as _os

    under = route_prefix(
        _os.environ.get("VECTRIXDB_EXTRACT_PREFIX", "") if prefix is None else prefix
    )
    console.print(f"  [dim]Service:[/dim] http://{host}:{port}{under}")
    console.print(f"  [dim]Routes:[/dim] http://{host}:{port}{under}/docs")
    console.print()

    from . import tracing

    if tracing.enabled():
        # This process is the service's own, so nothing else will set up where spans go.
        tracing.export_to_otlp()
    # "server: uvicorn" tells whoever finds the port what to look up.
    uvicorn.run(application, host=host, port=port, server_header=False)


@app.command()
def info(
    path: Optional[str] = typer.Argument(None, help=_PATH_HELP),
    env_file: Optional[str] = typer.Option(None, "--env-file", help=_ENV_FILE_HELP),
):
    """Show database information."""
    path = _settings(path, env_file)
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
    path: Optional[str] = typer.Argument(None, help=_PATH_HELP),
    env_file: Optional[str] = typer.Option(None, "--env-file", help=_ENV_FILE_HELP),
):
    """List all collections."""
    path = _settings(path, env_file)
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
    path: Optional[str] = typer.Option(None, "--path", "-d", help=_PATH_HELP),
    env_file: Optional[str] = typer.Option(None, "--env-file", help=_ENV_FILE_HELP),
    metric: str = typer.Option("cosine", "--metric", "-m", help="Distance metric"),
):
    """Create a new collection."""
    path = _settings(path, env_file)
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
    path: Optional[str] = typer.Option(None, "--path", "-d", help=_PATH_HELP),
    env_file: Optional[str] = typer.Option(None, "--env-file", help=_ENV_FILE_HELP),
    force: bool = typer.Option(False, "--force", "-f", help="Skip confirmation"),
):
    """Delete a collection."""
    path = _settings(path, env_file)
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
def ingest(
    sources: list[str] = typer.Argument(..., help="Files or directories to ingest"),
    name: str = typer.Option("docs", "--name", "-n", help="Collection name"),
    path: Optional[str] = typer.Option(None, "--path", "-d", help=_PATH_HELP),
    env_file: Optional[str] = typer.Option(None, "--env-file", help=_ENV_FILE_HELP),
    mode: str = typer.Option("dense", "--mode", help="dense, hybrid, ultimate or graph"),
    chunk: str = typer.Option(
        "recursive", "--chunk", help="recursive, sentence, semantic, markdown or fixed"
    ),
    chunk_size: int = typer.Option(1000, "--chunk-size"),
    overlap: int = typer.Option(200, "--overlap"),
    parent_size: Optional[int] = typer.Option(
        None, "--parent-size", help="store sections for search --parents"
    ),
    dedupe: Optional[float] = typer.Option(
        None, "--dedupe", help="skip near-duplicates at or above this similarity"
    ),
    glob: str = typer.Option("*", "--glob", help="pattern for files inside directories"),
):
    """Load, chunk and index files: PDF, DOCX, HTML, Markdown, text."""
    path = _settings(path, env_file)
    from pathlib import Path as _Path

    from .easy import Vectrix

    files: list = []
    for source in sources:
        p = _Path(source)
        if p.is_dir():
            files.extend(sorted(f for f in p.rglob(glob) if f.is_file()))
        elif p.is_file():
            files.append(p)
        else:
            console.print(f"[red]Not found:[/red] {source}")
            raise typer.Exit(1)
    if not files:
        console.print("[yellow]Nothing to ingest.[/yellow]")
        raise typer.Exit(1)

    db = Vectrix(name, path=path, mode=cast(Any, mode))
    total = 0
    skipped = 0
    try:
        for f in files:
            try:
                n = db.add_document(
                    f,
                    chunk=chunk,
                    chunk_size=chunk_size,
                    overlap=overlap,
                    parent_size=parent_size,
                    dedupe=dedupe,
                    progress=False,
                )
            except Exception as exc:  # noqa: BLE001 - report and carry on
                console.print(f"[red]{f}[/red]: {exc}")
                continue
            report = db.last_add_report
            skipped += len(report.skipped) if report else 0
            total += n
            console.print(f"[green]{f}[/green]: {n} chunks")
    finally:
        db.close()
    console.print(
        f"Indexed {total:,} chunks from {len(files)} files into {name!r}"
        + (f", skipped {skipped} near-duplicates" if skipped else "")
        + "."
    )


@app.command()
def query(
    text: str = typer.Argument(..., help="Query text"),
    name: str = typer.Option("docs", "--name", "-n", help="Collection name"),
    path: Optional[str] = typer.Option(None, "--path", "-d", help=_PATH_HELP),
    env_file: Optional[str] = typer.Option(None, "--env-file", help=_ENV_FILE_HELP),
    limit: int = typer.Option(5, "--limit", "-k"),
    mode: Optional[str] = typer.Option(
        None, "--mode", help="dense, sparse, hybrid, ultimate, graph"
    ),
    parents: bool = typer.Option(False, "--parents", help="return the enclosing sections"),
    explain: bool = typer.Option(False, "--explain", help="show score components"),
    json_out: bool = typer.Option(False, "--json", help="print JSON instead of a table"),
):
    """Search a collection from the shell."""
    path = _settings(path, env_file)
    import json as _json

    from .easy import Vectrix

    db = Vectrix(name, path=path)
    try:
        results = db.search(
            text, limit=limit, mode=cast(Any, mode), parents=parents, explain=explain
        )
    finally:
        db.close()
    if json_out:
        console.print_json(_json.dumps(results.to_dict()))
        return
    table = Table(title=f"{results.mode} search: {text!r}", show_lines=True)
    table.add_column("score", justify="right", style="cyan", width=7)
    table.add_column("id", style="dim", width=18, overflow="fold")
    table.add_column("text", overflow="fold")
    for r in results:
        snippet = r.text if len(r.text) <= 300 else r.text[:297] + "..."
        cell = snippet
        if explain and r.explain:
            cell += (
                "\n[dim]"
                + ", ".join(
                    f"{k}={v:.3f}" if isinstance(v, float) else f"{k}={v}"
                    for k, v in r.explain.items()
                )
                + "[/dim]"
            )
        table.add_row(f"{r.score:.3f}", r.id, cell)
    console.print(table)
    if results.truncated:
        console.print(f"[yellow]{results.cut_count} results cut to fit the budget.[/yellow]")


@app.command()
def stats(
    name: str = typer.Option("docs", "--name", "-n", help="Collection name"),
    path: Optional[str] = typer.Option(None, "--path", "-d", help=_PATH_HELP),
    env_file: Optional[str] = typer.Option(None, "--env-file", help=_ENV_FILE_HELP),
):
    """Size, model and mode of a collection."""
    path = _settings(path, env_file)
    from pathlib import Path as _Path

    from .easy import Vectrix

    db = Vectrix(name, path=path, readonly=True)
    try:
        count = db.count()
        model = db.embedding_model or db.model_name
        mode = db.default_mode
        dimension = db.dimension
    finally:
        db.close()
    base = _Path(path)
    size = sum(f.stat().st_size for f in base.glob(f"{name}*") if f.is_file())
    table = Table(title=f"Collection {name!r}", show_header=False)
    table.add_column("Property", style="cyan")
    table.add_column("Value", style="green")
    table.add_row("Path", str(base))
    table.add_row("Documents", f"{count:,}")
    table.add_row("Mode", mode)
    table.add_row("Embedding model", str(model))
    table.add_row("Dimension", str(dimension))
    table.add_row("Size on disk", _format_size(size))
    console.print(table)


# ============================================================================
# VERSION, MCP, AND THE MODELS
# ============================================================================
#
# INPUT   the command line
# OUTPUT  the version; a collection served over MCP so an assistant can use it
#         as a tool; the models the package does not hold, fetched once so later
#         runs need no network; what is installed, with sizes
#
# A download happens only when asked, here: nothing fetches on first use.


@app.command()
def version():
    """Show version information."""
    from . import __version__

    console.print(Panel(BANNER, style="cyan", border_style="cyan"))
    console.print(f"[bold]VectrixDB[/bold] v{__version__}")
    console.print("[dim]Where vectors come alive[/dim]")


@app.command()
def mcp(
    name: str = typer.Option("default", help="Collection name"),
    path: Optional[str] = typer.Option(None, "--path", "-d", help=_PATH_HELP),
    env_file: Optional[str] = typer.Option(None, "--env-file", help=_ENV_FILE_HELP),
    mode: Optional[str] = typer.Option(None, help="dense, hybrid, ultimate or graph"),
    transport: str = typer.Option("stdio", help="stdio, sse or streamable-http"),
):
    """Serve a collection over MCP so an assistant can use it as a tool."""
    path = _settings(path, env_file)
    from .mcp_server import main

    argv = ["--name", name, "--path", path, "--transport", transport]
    if mode:
        argv += ["--mode", mode]
    main(argv)


@app.command("download-models")
def download_models(
    model_type: str = typer.Option(
        "all",
        "--type",
        "-t",
        help="all, dense, reranker, or a type an error message named, such as dense_en",
    ),
    force: bool = typer.Option(False, "--force", "-f", help="Re-download even if models exist"),
):
    """
    Fetch the models the package does not hold, once, so later runs need no network.

    English needs nothing from here: the package holds bge-small-en-v1.5 for
    dense search, the BM25 vocabulary, the ms-marco-MiniLM-L-12-v2 reranker
    and a ColBERT model. This fetches the rest:

    - dense: multilingual-e5-small (~113MB), for 100+ languages
    - reranker: mmarco-mMiniLMv2-L12-H384-v1 (~113MB), for 15+ languages
    - all: both, and the BM25 vocabulary if it is missing
    - masking: the spaCy model Presidio reads each language in
      VECTRIXDB_MASKING_LANGUAGES with, for masking names and addresses

    Example:
        vectrixdb download-models          # the multilingual pair
        vectrixdb download-models -t dense # just the multilingual dense model
    """
    console.print(Panel(BANNER, style="cyan", border_style="cyan"))
    console.print("[bold green]VectrixDB Model Setup[/bold green]")
    console.print()
    console.print("[dim]This is a one-time setup. After download, VectrixDB[/dim]")
    console.print("[dim]works completely offline with no network calls.[/dim]")
    console.print()

    if model_type == "masking":
        from .masking import download_models as masking_models, languages_from_env

        try:
            got = masking_models(languages_from_env())
        except Exception as exc:  # noqa: BLE001 - said to the person, whatever spaCy raised
            console.print(f"[red]{exc}[/red]")
            raise typer.Exit(1)
        console.print(f"[green]Presidio can now read {', '.join(got)}[/green]")
        return

    try:
        from .models import download_models as do_download, is_models_installed, get_models_dir

        # Check what's already installed. A type's own directory, never the
        # bundled English model standing in for it: that said "already
        # installed" to the command every multilingual error message names.
        if not force:
            if model_type == "all":
                if is_models_installed("all", exact=True):
                    console.print("[green]All models already installed![/green]")
                    console.print(f"[dim]Location: {get_models_dir()}[/dim]")
                    console.print("[dim]Use --force to re-download.[/dim]")
                    return
            else:
                if is_models_installed(model_type, exact=True):
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
        # Escaped: Rich reads [setup-models] as a markup tag and prints
        # "pip install vectrixdb" with the extra silently dropped.
        console.print(r"  pip install vectrixdb\[setup-models]")
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
            installed = is_models_installed(model_type, exact=True)
            status = "[green]Installed[/green]" if installed else "[red]Not installed[/red]"
            size = f"~{config.get('size_mb', '?')}MB"

            table.add_row(
                str(config.get("name", model_type)),
                model_type,
                size,
                status,
            )

        console.print(table)
        console.print()
        console.print(f"[dim]Models directory: {models_dir}[/dim]")

        if not is_models_installed("all"):
            console.print()
            console.print(
                "[yellow]Run 'vectrixdb download-models' to install missing models.[/yellow]"
            )

    except Exception as e:
        console.print(f"[red]Error:[/red] {e}")
        raise typer.Exit(1)


def _format_size(bytes: int) -> str:
    """Format bytes as human readable."""
    size = float(bytes)
    for unit in ["B", "KB", "MB", "GB", "TB"]:
        if size < 1024:
            return f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} PB"


# --- people ---------------------------------------------------------------------
#
# Run on the server, beside the database, with the same settings as the server.
# This is how the first admin is made, since there is no default one, and the
# way back in when an admin has lost their phone and their recovery codes. A
# link printed here reaches only whoever is at this terminal, which is why the
# server's own start-up message prints this command and never a link.


# ============================================================================
# PEOPLE: the server's own list
# ============================================================================
#
# INPUT   an address and a role
# OUTPUT  somebody added, with a one-time link for them to choose how they
#         sign in; somebody reset, with a new link, and signed out everywhere;
#         everybody listed, with how they sign in; somebody removed
#
# The first admin is made here, on the server, since there is nobody to sign
# in and make one. The list is who may sign in whichever way they come, so it
# is here with single sign-on alone as well; a link is only for the list's
# own ways.

people_app = typer.Typer(
    help="The people who may sign in, managed from the server itself.", no_args_is_help=True
)
app.add_typer(people_app, name="people")


def _signin_store(path: str, *, links: bool = False) -> Any:
    """The sign-in settings and file the server at ``path`` uses. ``links``: the command hands out a set-up link."""
    from .exceptions import ConfigurationError
    from .signin import SignInConfig, SignInStore

    config = SignInConfig.from_env(path)
    if not config.enabled or not ({"email", "oidc"} & set(config.methods)):
        raise ConfigurationError(
            "sign-in is not on here. Set VECTRIXDB_SIGNIN=oidc,email (or oidc, or email), VECTRIXDB_SIGNIN_SECRET and "
            "VECTRIXDB_PUBLIC_URL, the same as the server's."
        )
    if links and "email" not in config.methods:
        raise ConfigurationError(
            "email sign-in is not on here, so nobody has a passkey or an authenticator kept here to reset: they sign in "
            "with single sign-on. To give people ways of their own too, set VECTRIXDB_SIGNIN=oidc,email, the same as the server's."
        )
    return config, SignInStore(
        config.store_url or config.store_path, config.secrets, key=config.store_key
    )


def _people(path: str, *, links: bool = False) -> Any:
    from .exceptions import ConfigurationError

    try:
        return _signin_store(path, links=links)
    except ConfigurationError as exc:
        console.print(f"[red]{exc}[/red]", markup=True, highlight=False)
        raise typer.Exit(code=2)


def _print_link(config: Any, token: str, then: str) -> None:
    from .api.gateway import Gateway
    from .api.server import root_path_from_env
    from .signin.store import LINK_MINUTES

    # Written the way people reach the dashboard: through the gateway, when there is one.
    dashboard = Gateway.from_env(root=root_path_from_env()).address(
        "/dashboard/", config.public_url
    )
    console.print(
        f"{then} It works once, for {LINK_MINUTES} minutes:", markup=False, highlight=False
    )
    console.print(
        f"  {dashboard}#/enrol?token={token}", markup=False, highlight=False, soft_wrap=True
    )


def _record(config: Any, event: str, **fields: Any) -> None:
    from .signin.access import AccessLog

    AccessLog(config.access_log).record(event, by="server console", **fields)


@people_app.command("add")
def people_add(
    email: str = typer.Argument(..., help="Their work email address"),
    role: str = typer.Option("viewer", "--role", "-r", help="viewer, operator or admin"),
    path: Optional[str] = typer.Option(None, "--path", "-d", help=_PATH_HELP),
    env_file: Optional[str] = typer.Option(None, "--env-file", help=_ENV_FILE_HELP),
):
    """Add somebody, and print a one-time link for them to choose how they sign in."""
    path = _settings(path, env_file)
    from .exceptions import ConfigurationError

    config, store = _people(path)
    links = "email" in config.methods
    try:
        before = store.person(email)
        if before is not None and before.enrolled:
            console.print(
                f"{before.email} is already here, as {before.role}, and set up. To send them round again: vectrixdb people reset {before.email}",
                markup=False,
                highlight=False,
            )
            raise typer.Exit(code=1)
        person = store.put_person(email, role)
        token = store.new_link(person.email, "enrol") if links else None
        _record(
            config,
            "person_changed" if before is not None else "person_added",
            who=person.email,
            role=person.role,
        )
    except ConfigurationError as exc:
        console.print(f"[red]{exc}[/red]", highlight=False)
        raise typer.Exit(code=2)
    finally:
        store.close()
    article = "an" if person.role[0] in "aeiou" else "a"
    console.print(
        f"Added {person.email} as {article} {person.role}.", markup=False, highlight=False
    )
    if "oidc" in config.methods:
        console.print(
            "They sign in with single sign-on"
            + (", then may add an authenticator app from their account." if links else "."),
            markup=False,
            highlight=False,
        )
    if token is not None:
        if config.sso_pending:
            console.print(
                "Single sign-on is not set up yet, so they sign in with their email and a code until it is.",
                markup=False,
                highlight=False,
            )
        own = "a passkey or an authenticator app" if config.own_passkeys else "an authenticator app"
        _print_link(
            config,
            token,
            f"Or open this to set up {own} first."
            if "oidc" in config.methods
            else f"Open this to set up {own}.",
        )


@people_app.command("reset")
def people_reset(
    email: str = typer.Argument(..., help="Their work email address"),
    path: Optional[str] = typer.Option(None, "--path", "-d", help=_PATH_HELP),
    env_file: Optional[str] = typer.Option(None, "--env-file", help=_ENV_FILE_HELP),
):
    """Forget somebody's passkeys, authenticator and recovery codes, sign them out, and print a new set-up link."""
    path = _settings(path, env_file)
    config, store = _people(path, links=True)
    try:
        person = store.person(email)
        if person is None:
            console.print(
                f"Nobody by the address {email} is on this server. Add them with: vectrixdb people add {email}",
                markup=False,
                highlight=False,
            )
            raise typer.Exit(code=1)
        store.reset_authenticator(person.email)
        token = store.new_link(person.email, "enrol")
        _record(config, "authenticator_reset", who=person.email)
    finally:
        store.close()
    console.print(
        f"Removed the passkeys, authenticator and recovery codes for {person.email}, and signed them out everywhere.",
        markup=False,
        highlight=False,
    )
    _print_link(config, token, "Open this to set up a new way in.")


@people_app.command("list")
def people_list(
    path: Optional[str] = typer.Option(None, "--path", "-d", help=_PATH_HELP),
    env_file: Optional[str] = typer.Option(None, "--env-file", help=_ENV_FILE_HELP),
):
    """Everybody on this server's People list, and how they sign in."""
    path = _settings(path, env_file)
    import datetime

    config, store = _people(path)
    try:
        everyone = store.people()
    finally:
        store.close()
    if not everyone:
        console.print(
            "Nobody yet. Add the first admin with: vectrixdb people add you@company.com --role admin",
            markup=False,
            highlight=False,
        )
        return
    table = Table(show_header=True, header_style="bold")
    for heading in ("Email", "Role", "Signs in with", "Last sign-in"):
        table.add_column(heading)
    for person in everyone:
        ways = ["single sign-on"] if person.last_sso_at else []
        if person.passkeys:
            ways.append(f"{person.passkeys} passkey{'s' if person.passkeys != 1 else ''}")
        if person.authenticator:
            ways.append("authenticator app")
        last = (
            datetime.datetime.fromtimestamp(person.last_sign_in).strftime("%d %b %Y %H:%M")
            if person.last_sign_in
            else "never"
        )
        table.add_row(
            person.email,
            person.role + (" (disabled)" if person.disabled else ""),
            ", ".join(ways) or "not set up yet",
            last,
        )
    console.print(table)


@people_app.command("remove")
def people_remove(
    email: str = typer.Argument(..., help="Their work email address"),
    path: Optional[str] = typer.Option(None, "--path", "-d", help=_PATH_HELP),
    env_file: Optional[str] = typer.Option(None, "--env-file", help=_ENV_FILE_HELP),
):
    """Take somebody off the list. They are signed out and can no longer sign in."""
    path = _settings(path, env_file)
    config, store = _people(path)
    try:
        person = store.person(email)
        if person is None:
            console.print(
                f"Nobody by the address {email} is on this server.", markup=False, highlight=False
            )
            raise typer.Exit(code=1)
        if person.role == "admin" and store.admins() <= 1:
            console.print(
                "This is the only admin. Make somebody else an admin first: vectrixdb people add them@company.com --role admin",
                markup=False,
                highlight=False,
            )
            raise typer.Exit(code=1)
        store.remove_person(person.email)
        _record(config, "person_removed", who=person.email)
    finally:
        store.close()
    console.print(
        f"Removed {person.email}. They are signed out and can no longer sign in.",
        markup=False,
        highlight=False,
    )


# ============================================================================
# EMERGENCY SIGN-IN: the hash of its password
# ============================================================================
#
# INPUT   a new password, typed twice and never shown
# OUTPUT  its hash, for VECTRIXDB_BREAK_GLASS_PASSWORD_HASH, and nothing else
#
# The password goes in a key vault and its hash in the settings. The hash
# checks a password and cannot be read back as one.

glass_app = typer.Typer(
    help="Emergency sign-in, for while the usual sign-in is down.", no_args_is_help=True
)
app.add_typer(glass_app, name="break-glass")


@glass_app.command("hash")
def break_glass_hash_command(
    admin: Optional[str] = typer.Option(
        None, "--admin", help="The emergency admin's username, so the password may not hold it"
    ),
):
    """Make the hash of a new emergency password. The password is typed twice, never shown, and kept nowhere."""
    from .exceptions import ConfigurationError
    from .signin import BREAK_GLASS_MIN_PASSWORD, break_glass_hash

    password = typer.prompt(
        f"New emergency password, {BREAK_GLASS_MIN_PASSWORD} characters or more",
        hide_input=True,
        confirmation_prompt=True,
    )
    try:
        hashed = break_glass_hash(password, admin)
    except ConfigurationError as exc:
        console.print(f"[red]{exc}[/red]", highlight=False)
        raise typer.Exit(code=2)
    console.print(
        "Keep the password in a key vault. Set this, its hash, on the server:",
        markup=False,
        highlight=False,
    )
    console.print(
        f"  VECTRIXDB_BREAK_GLASS_PASSWORD_HASH={hashed}",
        markup=False,
        highlight=False,
        soft_wrap=True,
    )
    console.print(
        "A password works for one emergency: make a new one for the next.",
        markup=False,
        highlight=False,
    )


# --- keys -----------------------------------------------------------------------
#
# Named API keys, from the server itself. The dashboard makes them too; this is
# for a deployment script, and for a server whose people sign in with single
# sign-on and whose first key has to come from somewhere. The key is printed
# once, to whoever is at this terminal, and kept only as a hash.


# ============================================================================
# KEYS
# ============================================================================
#
# INPUT   a name, a role, collections, days, and a rate
# OUTPUT  a key made and shown once, kept only as a hash; every key not
#         revoked, with what it may do, what it reaches and when it was last
#         used; a key revoked
#
# Anything using a revoked key stops working at once.

keys_app = typer.Typer(
    help="Named API keys for scripts and apps, managed from the server itself.",
    no_args_is_help=True,
)
app.add_typer(keys_app, name="keys")


def _keys_store(path: str) -> Any:
    from .exceptions import ConfigurationError
    from .signin import SignInConfig, SignInStore

    try:
        config = SignInConfig.from_env(path)
        if not config.enabled:
            raise ConfigurationError(
                "sign-in is not on here, and named keys are kept with it. Set VECTRIXDB_SIGNIN, VECTRIXDB_SIGNIN_SECRET and "
                "VECTRIXDB_PUBLIC_URL, the same as the server's."
            )
        return config, SignInStore(
            config.store_url or config.store_path, config.secrets, key=config.store_key
        )
    except ConfigurationError as exc:
        console.print(f"[red]{exc}[/red]", markup=True, highlight=False)
        raise typer.Exit(code=2)


def _key_scope(key: Any) -> str:
    import datetime

    said = [", ".join(key.collections) if key.collections else "every collection"]
    if key.expires_at:
        day = datetime.datetime.fromtimestamp(key.expires_at).strftime("%d %b %Y")
        said.append(f"expired {day}" if key.expired else f"until {day}")
    if key.per_minute:
        said.append(f"{key.per_minute} a minute")
    return ", ".join(said)


@keys_app.command("add")
def keys_add(
    name: str = typer.Argument(..., help="What uses the key, so it is clear later: handbook-bot"),
    role: str = typer.Option("searcher", "--role", "-r", help="reader, searcher or operator"),
    collection: Optional[List[str]] = typer.Option(
        None,
        "--collection",
        "-c",
        help="A collection it may reach. Repeat for more. Left out, every collection.",
    ),
    days: Optional[int] = typer.Option(
        None, "--days", help="How many days it works for. Left out, until it is revoked."
    ),
    per_minute: Optional[int] = typer.Option(
        None, "--per-minute", help="Requests a minute it may make. Left out, the server's setting."
    ),
    path: Optional[str] = typer.Option(None, "--path", "-d", help=_PATH_HELP),
    env_file: Optional[str] = typer.Option(None, "--env-file", help=_ENV_FILE_HELP),
):
    """Make a key. It is shown once, here, and kept only as a hash."""
    import time

    from .exceptions import ConfigurationError

    path = _settings(path, env_file)
    config, store = _keys_store(path)
    try:
        if days is not None and days < 1:
            raise ConfigurationError("--days is a whole number of days, 1 or more")
        made, key = store.create_key(
            name,
            role,
            "server console",
            collections=collection or None,
            expires_at=time.time() + days * 86400 if days else None,
            per_minute=per_minute,
        )
        _record(config, "key_created", who=made.name, role=made.role, reason=_key_scope(made))
    except ConfigurationError as exc:
        console.print(f"[red]{exc}[/red]", markup=True, highlight=False)
        raise typer.Exit(code=2)
    finally:
        store.close()
    console.print(
        f"Made the key {made.name}: {made.role}, {_key_scope(made)}. Copy it now, it is not shown again:",
        markup=False,
        highlight=False,
    )
    console.print(f"  {key}", markup=False, highlight=False, soft_wrap=True)


@keys_app.command("list")
def keys_list(
    path: Optional[str] = typer.Option(None, "--path", "-d", help=_PATH_HELP),
    env_file: Optional[str] = typer.Option(None, "--env-file", help=_ENV_FILE_HELP),
):
    """Every key that has not been revoked: what it may do, what it reaches, and when it was last used."""
    import datetime

    path = _settings(path, env_file)
    _, store = _keys_store(path)
    try:
        keys = store.keys()
    finally:
        store.close()
    if not keys:
        console.print(
            "No keys yet. Make one with: vectrixdb keys add handbook-bot --collection handbook --days 90",
            markup=False,
            highlight=False,
        )
        return
    table = Table(show_header=True, header_style="bold")
    for heading in ("Id", "Name", "Role", "Reaches", "Last used"):
        table.add_column(heading)
    for key in keys:
        last = (
            datetime.datetime.fromtimestamp(key.last_used).strftime("%d %b %Y %H:%M")
            if key.last_used
            else "never"
        )
        table.add_row(key.key_id, key.name, key.role, _key_scope(key), last)
    console.print(table)


@keys_app.command("revoke")
def keys_revoke(
    key_id: str = typer.Argument(..., help="The key's id, from: vectrixdb keys list"),
    path: Optional[str] = typer.Option(None, "--path", "-d", help=_PATH_HELP),
    env_file: Optional[str] = typer.Option(None, "--env-file", help=_ENV_FILE_HELP),
):
    """Revoke a key. Anything using it stops working at once."""
    path = _settings(path, env_file)
    config, store = _keys_store(path)
    try:
        name = store.revoke_key(key_id)
        if name is None:
            console.print(
                f"No key with the id {key_id}. See them with: vectrixdb keys list",
                markup=False,
                highlight=False,
            )
            raise typer.Exit(code=1)
        _record(config, "key_revoked", who=name)
    finally:
        store.close()
    console.print(
        f"Revoked {name}. Anything using it has stopped working.", markup=False, highlight=False
    )


# --- golden data and evaluation --------------------------------------------------
#
# A golden file is questions whose answers are known: which documents hold
# them. "golden template" writes one to fill in from a collection's own
# documents; "evaluate" searches every question every way the collections can
# be searched, times it, ranks the ways and names three. The run is saved where
# the server's Evaluate pages read it. Nothing is switched.


# ============================================================================
# GOLDEN, SWEEP, EVALUATE, AND CHECK
# ============================================================================
#
# INPUT   a collection, a golden file, and the options
# OUTPUT  a template of rows to fill in; questions drafted with a chat model,
#         every one a draft to check; the documents that answer each question
#         proposed; the answer cut-off measured; the index built every way and
#         ranked; every golden question searched every way and three picked;
#         the settings tested before a start
#
# The evaluation commands, each a thin face over vectrixdb.evaluation.

golden_app = typer.Typer(
    help="Golden questions: the questions whose right answers you know.", no_args_is_help=True
)
app.add_typer(golden_app, name="golden")


@golden_app.command("template")
def golden_template_command(
    name: str = typer.Argument(..., help="Collection to sample documents from"),
    path: Optional[str] = typer.Option(None, "--path", "-d", help=_PATH_HELP),
    env_file: Optional[str] = typer.Option(None, "--env-file", help=_ENV_FILE_HELP),
    out: str = typer.Option("golden.jsonl", "--out", "-o", help="Where to write the rows"),
    count: int = typer.Option(50, "--count", "-n", help="How many documents to sample"),
    seed: int = typer.Option(0, "--seed", help="The same seed gives the same documents"),
):
    """Write rows to fill in: one sampled document each, its id already in expected."""
    path = _settings(path, env_file)
    from .easy import Vectrix
    from .evaluation import golden_template

    try:
        db = Vectrix(name, path=path, readonly=True)
    except Exception as exc:
        console.print(f"[red]Error:[/red] {exc}")
        raise typer.Exit(1)
    try:
        rows = golden_template(db, out, n=count, seed=seed)
    except ValueError as exc:
        console.print(f"[red]Error:[/red] {exc}")
        raise typer.Exit(1)
    finally:
        db.close()
    console.print(
        f"Wrote {len(rows)} rows to {out}. Each names a document in expected and starts it in hint: "
        "write a question that document answers in question. Empty rows are skipped when the file is read.",
        markup=False,
        highlight=False,
    )


@golden_app.command("write")
def golden_write_command(
    name: str = typer.Argument(..., help="Collection whose chunks the questions are written from"),
    path: Optional[str] = typer.Option(None, "--path", "-d", help=_PATH_HELP),
    env_file: Optional[str] = typer.Option(None, "--env-file", help=_ENV_FILE_HELP),
    out: str = typer.Option(
        "golden.jsonl",
        "--out",
        "-o",
        help="Where to write the rows; never over a file that is there",
    ),
    count: int = typer.Option(50, "--count", "-n", help="How many questions"),
    seed: int = typer.Option(
        0, "--seed", help="The same seed, with the same answers, gives the same rows"
    ),
    examples: Optional[str] = typer.Option(
        None,
        "--examples",
        help="A text file of questions people really ask, one a line, whose style is copied",
    ),
    scenario: str = typer.Option("", "--scenario", help="Who asks, in a few words"),
    task: str = typer.Option("", "--task", help="What they are after, in a few words"),
    evolve: int = typer.Option(
        1, "--evolve", help="How many times each question is made harder; 0 for none"
    ),
    cache: Optional[str] = typer.Option(
        None,
        "--cache",
        help="Where answers are kept as they come, so a stopped run starts where it stopped. Default: beside --out",
    ),
):
    """Draft golden questions with a chat model from the collection's own chunks, every one a draft to check."""
    path = _settings(path, env_file)
    from pathlib import Path as _Path

    from .easy import Vectrix
    from .evaluation import WriterUnavailable, write_golden

    if _Path(out).exists():
        console.print(
            f"[red]Error:[/red] {out} is already there, and questions somebody checked are not written over. Give another --out."
        )
        raise typer.Exit(1)
    asked = []
    if examples:
        try:
            asked = [
                line.strip()
                for line in _Path(examples).read_text(encoding="utf-8").splitlines()
                if line.strip()
            ]
        except OSError as exc:
            console.print(f"[red]Error:[/red] {exc}")
            raise typer.Exit(1)
    kept = _Path(path) / f"{name}.documents"
    try:
        # The kept Markdown, when there is some, says where each section starts.
        db = Vectrix(name, path=path, readonly=True, keep_source=True if kept.is_dir() else None)
    except Exception as exc:
        console.print(f"[red]Error:[/red] {exc}")
        raise typer.Exit(1)
    try:
        written = write_golden(
            db,
            out,
            n=count,
            seed=seed,
            examples=asked,
            scenario=scenario,
            task=task,
            evolve=evolve,
            cache=cache or f"{out}.answers.jsonl",
            progress=lambda done, wanted: console.print(
                f"  {done} of {wanted}", markup=False, highlight=False
            ),
        )
    except (ValueError, WriterUnavailable) as exc:
        console.print(f"[red]Error:[/red] {exc}", markup=False, highlight=False)
        raise typer.Exit(1)
    finally:
        db.close()
    console.print(written.summary(), markup=False, highlight=False)
    console.print(
        "Read each row against its page: correct the question, the reference or expected where they are wrong, "
        'and delete "draft" from the ones you checked.',
        markup=False,
        highlight=False,
    )


@golden_app.command("label")
def golden_label_command(
    name: str = typer.Argument(..., help="Collection that holds the documents"),
    questions: str = typer.Option(
        ...,
        "--questions",
        "-q",
        help="Questions with reference answers and no labels: a Bedrock evaluation dataset or this library's JSONL",
    ),
    path: Optional[str] = typer.Option(None, "--path", "-d", help=_PATH_HELP),
    env_file: Optional[str] = typer.Option(None, "--env-file", help=_ENV_FILE_HELP),
    out: str = typer.Option("golden.jsonl", "--out", "-o", help="Where to write the rows"),
    top: int = typer.Option(1, "--top", help="How many documents to propose for each question"),
):
    """Propose the documents that answer each question, for a person to check."""
    path = _settings(path, env_file)
    import json as _json
    from pathlib import Path as _Path

    from .easy import Vectrix
    from .evaluation import load_questions, suggest_expected

    if _Path(out).exists():
        console.print(
            f"[red]Error:[/red] {out} is already there, and labels somebody checked are not written over. Give another --out."
        )
        raise typer.Exit(1)
    try:
        asked = load_questions(questions)
        db = Vectrix(name, path=path, readonly=True)
    except Exception as exc:
        console.print(f"[red]Error:[/red] {exc}")
        raise typer.Exit(1)
    try:
        proposed = suggest_expected(db, asked, top=top)
    finally:
        db.close()
    rows = []
    for q in asked:
        row = q.to_dict()
        if not q.expected:
            # A proposal, and marked as one: searched with the reference
            # answer, which finds the document that says it and proves nothing.
            row["expected"], row["draft"] = proposed.get(q.id or q.text, []), True
        rows.append(row)
    _Path(out).write_text(
        "".join(_json.dumps(r, ensure_ascii=False) + "\n" for r in rows), encoding="utf-8"
    )
    drafts = sum(1 for r in rows if r.get("draft"))
    console.print(
        f"Wrote {len(rows)} rows to {out}, {drafts} with a proposed label marked draft. "
        "Read each against its document, correct expected where it is wrong, and delete the draft mark from the ones you checked.",
        markup=False,
        highlight=False,
    )


@golden_app.command("cutoff")
def golden_cutoff_command(
    name: str = typer.Argument(..., help="Collection to search"),
    golden: str = typer.Option(
        ..., "--golden", "-g", help="Golden file: a path, s3://bucket/key or a Blob address"
    ),
    path: Optional[str] = typer.Option(None, "--path", "-d", help=_PATH_HELP),
    env_file: Optional[str] = typer.Option(None, "--env-file", help=_ENV_FILE_HELP),
    unanswerable: Optional[str] = typer.Option(
        None,
        "--unanswerable",
        "-u",
        help="A text file of questions the documents do not answer, one a line",
    ),
    mode: str = typer.Option(
        "dense",
        "--mode",
        help="How the questions are searched; the cut-off belongs to that way of searching",
    ),
    measure: str = typer.Option(
        "similarity",
        "--measure",
        help="similarity, the cosine alone, or relevance, the reranker's verdict where one ran",
    ),
):
    """Measure the relevance below which the top result is more likely a near miss than the answer."""
    path = _settings(path, env_file)
    from pathlib import Path as _Path

    from .easy import Vectrix
    from .evaluation import answer_cutoff, read_golden

    try:
        gold = read_golden(golden)
        outside = (
            [
                line.strip()
                for line in _Path(unanswerable).read_text(encoding="utf-8").splitlines()
                if line.strip()
            ]
            if unanswerable
            else []
        )
        db = Vectrix(name, path=path, mode=cast(Any, mode), readonly=True)
    except Exception as exc:
        console.print(f"[red]Error:[/red] {exc}")
        raise typer.Exit(1)
    try:
        found = answer_cutoff(db, gold.labelled, unanswerable=outside, measure=measure, mode=mode)
    except ValueError as exc:
        console.print(f"[red]Error:[/red] {exc}")
        raise typer.Exit(1)
    finally:
        db.close()
    console.print(
        f"{found['answers']} answers and {found['near_misses']} near misses at the top, from {found['questions']} questions "
        f"and {found['unanswerable']} the documents do not answer, with {found['model']}",
        markup=False,
        highlight=False,
    )
    if found["cutoff"] is None:
        console.print(f"No cut-off: {found['reason']}", markup=False, highlight=False)
        raise typer.Exit(1)
    table = Table(title=f"Answer at or above this {measure}")
    for column in ("Cut-off", "Right when it answers", "Answers kept", "Near misses declined"):
        table.add_column(column, justify="right")
    for row in found["table"]:
        mark = " *" if row["cutoff"] == found["cutoff"] else ""
        table.add_row(
            f"{row['cutoff']:.3f}{mark}",
            f"{row['precision']:.0%}" if row["precision"] is not None else "",
            f"{row['answered']:.0%}",
            f"{row['declined']:.0%}",
        )
    console.print(table)
    console.print(
        f"Cut-off {found['cutoff']:.3f}. An answer outscores a near miss {found['separation']:.0%} of the time; "
        "under about 75% the two are too mixed for any cut-off to be worth acting on.",
        markup=False,
        highlight=False,
    )


@app.command("sweep")
def sweep_command(
    files: List[str] = typer.Argument(..., help="The original documents, or folders of them"),
    golden: str = typer.Option(
        ..., "--golden", "-g", help="Golden file whose expected names documents"
    ),
    chunk: List[str] = typer.Option(
        ["recursive", "markdown"], "--chunk", help="A chunker to try; give it again for another"
    ),
    size: List[int] = typer.Option(
        [500, 1000], "--size", help="A chunk size to try, in characters"
    ),
    overlap: List[int] = typer.Option([100], "--overlap", help="An overlap to try, in characters"),
    heading: str = typer.Option(
        "both", "--heading", help="Whether the headings go to the embedder: yes, no, or both"
    ),
    mode: List[str] = typer.Option(
        ["dense"], "--mode", help="A way of searching each build: dense, hybrid, ultimate"
    ),
    out: Optional[str] = typer.Option(
        None, "--out", "-o", help="Write the whole report here as JSON"
    ),
):
    """Build the index every way asked, from the originals, and rank the ways by what the golden questions find."""
    import json as _json
    from pathlib import Path as _Path

    from .evaluation import read_golden, sweep, sweep_markdown

    headings = {"yes": [True], "no": [False], "both": [False, True]}.get(heading.lower())
    if headings is None:
        console.print(f"[red]Error:[/red] --heading is yes, no or both, got {heading!r}")
        raise typer.Exit(1)
    sources: List[str] = []
    for given in files:
        here = _Path(given)
        sources += (
            sorted(str(f) for f in here.rglob("*") if f.is_file()) if here.is_dir() else [str(here)]
        )
    try:
        gold = read_golden(golden)
        report = sweep(
            sources,
            gold.labelled,
            chunk=chunk,
            chunk_size=size,
            overlap=overlap,
            embed_heading=headings,
            search=[{"mode": m} if m == "dense" else {"mode": m, "rerank": False} for m in mode],
            progress=lambda row, n, total: console.print(
                f"  {n}/{total} {row['chunk']} {row['chunk_size']}, nDCG {row['ndcg']:.3f}",
                markup=False,
                highlight=False,
            ),
        )
    except (OSError, ValueError, ImportError) as exc:
        console.print(f"[red]Error:[/red] {exc}")
        raise typer.Exit(1)
    console.print(sweep_markdown(report), markup=False, highlight=False)
    if out:
        _Path(out).write_text(_json.dumps(report, indent=2), encoding="utf-8")
        console.print(f"Wrote {out}.", markup=False, highlight=False)
    console.print(
        "Nothing was changed. The builds were scratch copies; to use the best one, ingest with those settings.",
        markup=False,
        highlight=False,
    )


@app.command()
def evaluate(
    names: List[str] = typer.Argument(
        ..., help="Collections to evaluate, the same documents on each"
    ),
    golden: str = typer.Option(
        ..., "--golden", "-g", help="Golden file: a path, s3://bucket/key or a Blob address"
    ),
    path: Optional[str] = typer.Option(None, "--path", "-d", help=_PATH_HELP),
    env_file: Optional[str] = typer.Option(None, "--env-file", help=_ENV_FILE_HELP),
    save_to: Optional[str] = typer.Option(
        None,
        "--save-to",
        help="Where runs go: a folder, s3://bucket/prefix or a Blob address. Default: VECTRIXDB_EVALUATIONS, or <path>/evaluations, which is where the server reads them",
    ),
    mode: str = typer.Option(
        "ultimate",
        "--mode",
        help="The most a collection is opened for; ultimate includes every local method",
    ),
    balance: float = typer.Option(
        4, "--balance", help="best_for_balance: the fastest within this many points of the most"
    ),
    time_points: float = typer.Option(
        8, "--time", help="best_for_time: the fastest within this many points of the most"
    ),
):
    """Search every golden question every way the collections can be searched, and pick three."""
    path = _settings(path, env_file)
    import os as _os
    import warnings
    from pathlib import Path as _Path

    from .easy import Vectrix
    from .evaluation import (
        PICKS,
        MissingDocumentsWarning,
        Target,
        evaluate as run_evaluation,
        read_golden,
    )

    try:
        gold = read_golden(golden)
    except (OSError, ValueError, ImportError) as exc:
        console.print(f"[red]Error:[/red] {exc}")
        raise typer.Exit(1)
    handles = []
    try:
        for name in names:
            handles.append(
                Target(
                    Vectrix(name, path=path, mode=cast(Any, mode), readonly=True),
                    name=None if len(names) == 1 else name,
                    collection=name,
                )
            )
    except Exception as exc:
        for target in handles:
            target.db.close()
        console.print(f"[red]Error:[/red] {exc}")
        raise typer.Exit(1)
    # Where the server reads runs, so a run made here is on its Evaluate page.
    where = (
        save_to
        or _os.environ.get("VECTRIXDB_EVALUATIONS", "").strip()
        or str(_Path(path) / "evaluations")
    )
    console.print(
        f"{len(gold.labelled)} questions from {golden}"
        + (f", {gold.unfilled} template rows still empty" if gold.unfilled else ""),
        markup=False,
        highlight=False,
    )

    def say(message: Any, *_where: Any, **_how: Any) -> None:
        # The missing-documents check runs before any searching, so this
        # prints while there is still time to stop and fix the golden file.
        console.print(f"Warning: {message}", markup=False, highlight=False)

    try:
        with warnings.catch_warnings():
            warnings.simplefilter("always", MissingDocumentsWarning)
            warnings.showwarning = say
            report = run_evaluation(
                handles,
                gold,
                save_to=where,
                balance=balance,
                time_points=time_points,
                progress=lambda setup, n, total: console.print(
                    f"  {n}/{total} {setup['method_label']} on {setup['engine']}",
                    markup=False,
                    highlight=False,
                ),
            )
    except (ValueError, ImportError) as exc:
        console.print(f"[red]Error:[/red] {exc}")
        raise typer.Exit(1)
    finally:
        for target in handles:
            target.db.close()

    table = Table(title="Every setup, ranked")
    table.add_column("#", justify="right")
    table.add_column("Setup")
    table.add_column("Top 10", justify="right")
    table.add_column("First", justify="right")
    table.add_column("Median", justify="right")
    for s in report["setups"]:
        if s.get("error"):
            continue
        models = " + ".join(m["label"] for m in s["models"]) or "Words only"
        summary = s["summary"]
        table.add_row(
            str(s["rank"]),
            f"{s['method_label']} on {s['engine']}, {models}",
            f"{summary['found']['10']:.0%}",
            f"{summary['found']['1']:.0%}",
            f"{summary['median_ms']:.0f} ms",
        )
    console.print(table)
    failed = [s for s in report["setups"] if s.get("error")]
    for s in failed:
        console.print(
            f"Could not run {s['method_label']} on {s['engine']}: {s['error']}",
            markup=False,
            highlight=False,
        )
    by_key = {s["key"]: s for s in report["setups"]}
    for pick in PICKS:
        key = report["picks"].get(pick)
        if key:
            s = by_key[key]
            console.print(
                f"{pick}: {s['method_label']} on {s['engine']}, {s['summary']['found']['10']:.0%} in the top 10, {s['summary']['median_ms']:.0f} ms",
                markup=False,
                highlight=False,
            )
    console.print(
        f"Saved run {report['id']} to {where}. Nothing was switched: choose, then change the settings.",
        markup=False,
        highlight=False,
    )


@app.command()
def check(
    path: Optional[str] = typer.Option(None, "--path", "-d", help=_PATH_HELP),
    env_file: Optional[str] = typer.Option(None, "--env-file", help=_ENV_FILE_HELP),
    template: bool = typer.Option(
        False,
        "--template",
        help="Print a settings file with every setting in it, to fill in, and stop",
    ),
    url: Optional[str] = typer.Option(
        None,
        "--url",
        help="Check a running server from outside instead, through its gateway: https://apim.company.com/vectrixdb",
    ),
    prefix: Optional[str] = typer.Option(
        None,
        "--prefix",
        help="With --url: the path every route lives under, as VECTRIXDB_PREFIX. Left out, the setting",
    ),
    gateway_paths: Optional[str] = typer.Option(
        None,
        "--gateway-paths",
        help="With --url: the gateway path each route is published under, as VECTRIXDB_GATEWAY_PATHS: api/v1=/files/search,auth=/files/auth. Left out, the setting",
    ),
):
    """Test the settings before a start: what a start would refuse, and what looks wrong."""
    if template:
        from .settings import template as settings_template

        typer.echo(settings_template(), nl=False)
        return
    if url:
        import os as _os

        from rich.markup import escape as _escape

        from .api.gateway import Gateway
        from .exceptions import ConfigurationError
        from .probe import probe

        _env_file(env_file)
        # The server's own settings, unless the command is told otherwise, so each route is asked where it is published.
        given = dict(_os.environ)
        if prefix is not None:
            given["VECTRIXDB_PREFIX"] = prefix
        if gateway_paths is not None:
            given["VECTRIXDB_GATEWAY_PATHS"] = gateway_paths
        try:
            gateway = Gateway.from_env(given)
        except ConfigurationError as exc:
            console.print(f"[red]{_escape(str(exc))}[/red]")
            raise typer.Exit(code=2)
        console.print(
            f"Asking {url} what a caller would. Every request is a GET.",
            markup=False,
            highlight=False,
        )
        console.print()
        _marks = {
            "ok": "[green]ok[/green]   ",
            "warn": "[yellow]warn[/yellow] ",
            "error": "[red]error[/red]",
        }
        answers = probe(
            url,
            gateway=gateway if gateway.dressed or gateway.token_header != "authorization" else None,
        )
        for finding in answers:
            console.print(f"  {_marks[finding.level]}  {_escape(finding.text)}", highlight=False)
        bad = sum(1 for f in answers if f.level == "error")
        console.print()
        if bad:
            console.print(
                f"{bad} error{'s' if bad != 1 else ''}. Callers on the other side of the gateway are not getting the server they were promised.",
                markup=False,
                highlight=False,
            )
            raise typer.Exit(code=1)
        console.print(
            "No errors. What a caller reaches is the server.", markup=False, highlight=False
        )
        return
    _env_file(env_file)
    from .check import run

    where = _data_path(path)
    findings = run(where)
    source = f"{env_file} and the environment" if env_file else "the environment"
    console.print(f"Settings for {where}, from {source}", markup=False, highlight=False)
    console.print()
    marks = {
        "ok": "[green]ok[/green]   ",
        "warn": "[yellow]warn[/yellow] ",
        "error": "[red]error[/red]",
    }
    from rich.markup import escape

    for finding in findings:
        console.print(
            f"  {marks[finding.level]}  {escape(finding.area):<12} {escape(finding.text)}",
            highlight=False,
        )
    errors = sum(1 for f in findings if f.level == "error")
    warnings = sum(1 for f in findings if f.level == "warn")
    console.print()
    if errors:
        console.print(
            f"{errors} error{'s' if errors != 1 else ''}, {warnings} warning{'s' if warnings != 1 else ''}. A start would fail or misbehave until the errors are fixed.",
            markup=False,
            highlight=False,
        )
        raise typer.Exit(code=1)
    console.print(
        f"No errors{f', {warnings} warning' + ('s' if warnings != 1 else '') if warnings else ''}. Ready to start.",
        markup=False,
        highlight=False,
    )


# ============================================================================
# SOURCES: the feeds and pages a collection keeps up with
# ============================================================================
#
# INPUT   an address and how often; a collection; a source's id or address
# OUTPUT  the source kept; the list; the source forgotten; every source that
#         is due read again, only what changed written, a line a source, and
#         exit code 1 when anything failed, for the scheduler to see
#
# For collections on this machine's disk, as ingest is. A server whose index
# lives elsewhere is refreshed through its own route, which a scheduler calls.

sources_app = typer.Typer(
    help="Feeds and pages a collection keeps up with, read again on a schedule.",
    no_args_is_help=True,
)
app.add_typer(sources_app, name="sources")


def _say(text: str) -> None:
    console.print(text, markup=False, highlight=False, soft_wrap=True)


def _collections_with_sources(path: str) -> List[str]:
    """The collections at ``path`` that keep up with a source, read from the database's own file without opening any."""
    from pathlib import Path as _Path

    from .sources import SOURCE, local_store

    if not (_Path(path) / "_vectrixdb.db").is_file():
        return []
    store = local_store(path)
    try:
        return sorted({str(r.data.get("collection") or "") for r in store.query(SOURCE)} - {""})
    finally:
        store.close()


@sources_app.command("add")
def sources_add(
    address: str = typer.Argument(
        ..., help="The feed's or the page's address. Write ${NAME} where a secret goes."
    ),
    name: str = typer.Option("docs", "--name", "-n", help="Collection name"),
    every: str = typer.Option("6h", "--every", help="How often it is read: 30m, 6h, 1d, 1w"),
    kind: Optional[str] = typer.Option(
        None, "--kind", help="feed or page. Left out, it is fetched once to tell"
    ),
    articles: bool = typer.Option(
        False, "--articles", help="A feed: index the article each entry links to"
    ),
    no_transcribe: bool = typer.Option(
        False, "--no-transcribe", help="A podcast: its show notes, even with an audio engine"
    ),
    delete_when_gone: bool = typer.Option(
        False, "--delete-when-gone", help="A page: remove its chunks when it answers 404 or 410"
    ),
    path: Optional[str] = typer.Option(None, "--path", "-d", help=_PATH_HELP),
    env_file: Optional[str] = typer.Option(None, "--env-file", help=_ENV_FILE_HELP),
):
    """Keep a collection up with a feed or a page. Nothing is written until a refresh."""
    from .easy import Vectrix
    from .exceptions import ConfigurationError, DependencyError, ExtractionError
    from .sources import every_text

    path = _settings(path, env_file)
    options: dict = {}
    if articles:
        options["articles"] = True
    if no_transcribe:
        options["transcribe"] = False
    if delete_when_gone:
        options["delete_when_gone"] = True
    db = Vectrix(name, path=path)
    try:
        info = db.sources.add(address, every, kind=kind, by="command line", **options)
    except (ConfigurationError, DependencyError, ExtractionError) as exc:
        _say(str(exc))
        raise typer.Exit(code=2)
    finally:
        db.close()
    _say(
        f"{name}: added the {info.kind} {info.address}, read every {every_text(info.every)}, id {info.id}. "
        "Nothing is written until: vectrixdb sources refresh"
    )


@sources_app.command("list")
def sources_list(
    name: str = typer.Option("docs", "--name", "-n", help="Collection name"),
    path: Optional[str] = typer.Option(None, "--path", "-d", help=_PATH_HELP),
    env_file: Optional[str] = typer.Option(None, "--env-file", help=_ENV_FILE_HELP),
):
    """The feeds and pages a collection keeps up with, and how each last went."""
    from .easy import Vectrix
    from .sources import every_text

    path = _settings(path, env_file)
    db = Vectrix(name, path=path)
    try:
        found = db.sources.list()
    finally:
        db.close()
    if not found:
        _say(
            f"{name} keeps up with no sources. Add one: vectrixdb sources add <address> --name {name}"
        )
        return
    table = Table(title=f"Sources of {name}")
    for column in ("id", "kind", "address", "every", "last read", "status", "documents"):
        table.add_column(column)
    for s in found:
        status = s.last_status or "not read yet"
        if s.last_error:
            status += f": {s.last_error}"
        table.add_row(
            s.id,
            s.kind,
            s.address,
            every_text(s.every),
            s.last_refresh or "",
            status,
            str(s.documents),
        )
    console.print(table)


@sources_app.command("remove")
def sources_remove(
    source: str = typer.Argument(..., help="The source's id or its address"),
    name: str = typer.Option("docs", "--name", "-n", help="Collection name"),
    delete_documents: bool = typer.Option(
        False, "--delete-documents", help="Take the documents it wrote too"
    ),
    path: Optional[str] = typer.Option(None, "--path", "-d", help=_PATH_HELP),
    env_file: Optional[str] = typer.Option(None, "--env-file", help=_ENV_FILE_HELP),
):
    """Stop keeping up with a source. Its documents stay unless --delete-documents."""
    from .easy import Vectrix

    path = _settings(path, env_file)
    db = Vectrix(name, path=path)
    try:
        removed = db.sources.remove(source, delete_documents=delete_documents)
    finally:
        db.close()
    if not removed:
        _say(f"{name} has no source {source}. See: vectrixdb sources list --name {name}")
        raise typer.Exit(code=1)
    _say(
        f"{name}: removed {source}"
        + (", and the documents it wrote." if delete_documents else "; its documents stay.")
    )


@sources_app.command("refresh")
def sources_refresh(
    name: Optional[str] = typer.Option(
        None,
        "--name",
        "-n",
        help="Collection name. Left out, every collection here that keeps up with a source",
    ),
    force: bool = typer.Option(False, "--force", help="Every source, not only the ones due"),
    max_items: int = typer.Option(50, "--max-items", help="Entries written per source this time"),
    path: Optional[str] = typer.Option(None, "--path", "-d", help=_PATH_HELP),
    env_file: Optional[str] = typer.Option(None, "--env-file", help=_ENV_FILE_HELP),
):
    """Read the sources that are due and write only what changed. Run it from cron or a scheduled job."""
    from .easy import Vectrix

    path = _settings(path, env_file)
    names = [name] if name else _collections_with_sources(path)
    if not names:
        _say("No collection here keeps up with a source.")
        return
    failed = 0
    for each in names:
        db = Vectrix(each, path=path)
        try:
            report = db.sources.refresh(force, max_items=max_items)
        finally:
            db.close()
        for outcome in report.sources:
            line = (
                f"{each}: {outcome.address}: {outcome.status}, {len(outcome.added)} added, "
                f"{len(outcome.updated)} updated, {outcome.unchanged} unchanged"
            )
            if outcome.removed or outcome.gone:
                line += f", {len(outcome.removed)} removed, {len(outcome.gone)} gone"
            if outcome.waiting:
                line += f", {outcome.waiting} waiting"
            if outcome.reason:
                line += f". {outcome.reason}"
            _say(line)
            for failure in outcome.failed:
                _say(f"  {failure['item']}: {failure['reason']}")
            for note in outcome.notes:
                _say(f"  {note}")
        _say(f"{each}: {report}")
        failed += len(report.failed)
    if failed:
        raise typer.Exit(code=1)


# ============================================================================
# MAIN SCRIPT
# ============================================================================
#
# INPUT   sys.argv
# OUTPUT  the app run
#
# python -m vectrixdb.cli, the same as the vectrixdb command.


if __name__ == "__main__":
    app()
