"""
VectrixDB Setup Script

Usage:
    python setup.py              # Full setup (backend + frontend)
    python setup.py --backend    # Backend only
    python setup.py --frontend   # Frontend only

Author: Daddy Nyame Owusu - Boakye
"""

import os
import subprocess
import sys
import argparse
from pathlib import Path

# Colors
class Colors:
    CYAN = '\033[96m'
    GREEN = '\033[92m'
    YELLOW = '\033[93m'
    RED = '\033[91m'
    BOLD = '\033[1m'
    RESET = '\033[0m'

# Enable colors on Windows
if sys.platform == "win32":
    os.system("")

BANNER = f"""
{Colors.CYAN}{Colors.BOLD}
╔╗  ╔╗         ╔╗          ╔═══╗ ╔══╗
║╚╗╔╝║         ║║          ╚╗╔╗║ ║╔╗║
╚╗║║╔╝╔══╗ ╔══╗║╚═╗╔═╗╔╗╔╗  ║║║║ ║╚╝╚╗
 ║╚╝║ ║╔╗║ ║╔═╝║╔╗║║╔╝╠╣╠╣  ║║║║ ║╔═╗║
 ╚╗╔╝ ║║═╣ ║╚═╗║║║║║║ ║║║║ ╔╝╚╝║ ║╚═╝║
  ╚╝  ╚══╝ ╚══╝╚╝╚╝╚╝ ╚╝╚╝ ╚═══╝ ╚═══╝
       Where vectors come alive
{Colors.RESET}
"""


def print_step(msg: str):
    print(f"\n{Colors.CYAN}[*]{Colors.RESET} {msg}")


def print_success(msg: str):
    print(f"{Colors.GREEN}[✓]{Colors.RESET} {msg}")


def print_warning(msg: str):
    print(f"{Colors.YELLOW}[!]{Colors.RESET} {msg}")


def print_error(msg: str):
    print(f"{Colors.RED}[✗]{Colors.RESET} {msg}")


def run_command(cmd: list, cwd: Path = None) -> bool:
    """Run a command and return success status."""
    try:
        subprocess.run(cmd, cwd=cwd, check=True)
        return True
    except subprocess.CalledProcessError:
        return False
    except FileNotFoundError:
        print_error(f"Command not found: {cmd[0]}")
        return False


def setup_backend(project_dir: Path) -> bool:
    """Set up the Python backend."""
    print_step("Setting up Python backend...")

    # Create virtual environment
    venv_path = project_dir / "venv"
    if not venv_path.exists():
        print_step("Creating virtual environment...")
        if not run_command([sys.executable, "-m", "venv", str(venv_path)]):
            print_error("Failed to create virtual environment")
            return False
        print_success("Virtual environment created")

    # Get pip path
    if sys.platform == "win32":
        pip = venv_path / "Scripts" / "pip.exe"
        python = venv_path / "Scripts" / "python.exe"
    else:
        pip = venv_path / "bin" / "pip"
        python = venv_path / "bin" / "python"

    # Upgrade pip
    print_step("Upgrading pip...")
    run_command([str(python), "-m", "pip", "install", "--upgrade", "pip"])

    # Install package in development mode
    print_step("Installing VectrixDB package...")
    if not run_command([str(pip), "install", "-e", "."]):
        # Try without extras if it fails
        print_warning("Trying minimal install...")
        if not run_command([str(pip), "install", "-e", ".", "--no-deps"]):
            print_error("Failed to install package")
            return False

    # Install core dependencies
    print_step("Installing dependencies...")
    deps = [
        "numpy",
        "fastapi",
        "uvicorn[standard]",
        "pydantic",
        "aiosqlite",
        "httpx",
        "rich",
        "typer",
        "websockets",
        "orjson",
    ]

    for dep in deps:
        run_command([str(pip), "install", dep])

    # Try to install usearch
    print_step("Installing vector index backend...")
    if not run_command([str(pip), "install", "usearch"]):
        print_warning("usearch not available, trying hnswlib...")
        if not run_command([str(pip), "install", "hnswlib"]):
            print_warning("Neither usearch nor hnswlib installed - install manually")

    print_success("Backend setup complete!")
    return True


def setup_frontend(project_dir: Path) -> bool:
    """Set up the React frontend."""
    print_step("Setting up React frontend...")

    dashboard_dir = project_dir / "dashboard"

    # Check for npm/pnpm
    npm_cmd = "pnpm" if run_command(["pnpm", "--version"]) else "npm"

    # Install dependencies
    print_step(f"Installing frontend dependencies with {npm_cmd}...")
    if not run_command([npm_cmd, "install"], cwd=dashboard_dir):
        print_error("Failed to install frontend dependencies")
        return False

    print_success("Frontend setup complete!")
    return True


def main():
    parser = argparse.ArgumentParser(description="VectrixDB Setup")
    parser.add_argument("--backend", action="store_true", help="Setup backend only")
    parser.add_argument("--frontend", action="store_true", help="Setup frontend only")
    args = parser.parse_args()

    print(BANNER)

    project_dir = Path(__file__).parent

    # If neither specified, do both
    do_backend = args.backend or (not args.backend and not args.frontend)
    do_frontend = args.frontend or (not args.backend and not args.frontend)

    success = True

    if do_backend:
        if not setup_backend(project_dir):
            success = False

    if do_frontend:
        if not setup_frontend(project_dir):
            success = False

    if success:
        print(f"\n{Colors.GREEN}{Colors.BOLD}Setup complete!{Colors.RESET}\n")
        print("To start VectrixDB:")
        print(f"  {Colors.CYAN}# Activate virtual environment{Colors.RESET}")
        if sys.platform == "win32":
            print(f"  .\\venv\\Scripts\\activate")
        else:
            print(f"  source venv/bin/activate")
        print()
        print(f"  {Colors.CYAN}# Start the server{Colors.RESET}")
        print(f"  vectrixdb serve --port 7337")
        print()
        print(f"  {Colors.CYAN}# Or start frontend dev server{Colors.RESET}")
        print(f"  cd dashboard && npm run dev")
    else:
        print(f"\n{Colors.RED}Setup completed with errors{Colors.RESET}")
        sys.exit(1)


if __name__ == "__main__":
    main()
