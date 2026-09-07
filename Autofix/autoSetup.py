#!/usr/bin/env python3
"""Install and check Autofix code dependencies.

This setup script is intentionally based on the current Python source shape:

- Python imports are standard-library-only, so requirements.txt is the pip source
  of truth and is currently empty except comments.
- Runtime code calls external executables through subprocess. Those executables
  must exist on PATH or be configured later in the management console.

Default behavior:

1. Check Python version.
2. Install Python packages from requirements.txt.
3. Install or check required runtime commands: git, lark-cli, rg.
4. Ensure at least one editable AI CLI exists. If none is found, try installing
   Codex CLI as the default editable CLI.

Examples:

    python3 Autofix/autoSetup.py
    python3 Autofix/autoSetup.py --dry-run
    python3 Autofix/autoSetup.py --ai-cli claude
    python3 Autofix/autoSetup.py --ai-cli none
"""

from __future__ import annotations

import argparse
import platform
import shutil
import subprocess
import sys
from pathlib import Path


APP_DIR = Path(__file__).resolve().parent
REQUIREMENTS_FILE = APP_DIR / "requirements.txt"
TOOLS_DIR = APP_DIR / "tools"
PAGE_POLISH_SKILL_FILE = APP_DIR / "skills" / "html-page-polish-skill.md"
MIN_PYTHON = (3, 9)

REQUIRED_COMMANDS = {
    "git": "Git is required to create worktrees, branches, diffs, and commits.",
    "lark-cli": "lark-cli is required to read Feishu messages, reply, and update Base records.",
    "rg": "ripgrep is used by the default context collection command.",
}

BREW_PACKAGES = {
    "git": "git",
    "rg": "ripgrep",
    "node": "node",
}

APT_PACKAGES = {
    "git": "git",
    "rg": "ripgrep",
    "node": "nodejs",
    "npm": "npm",
}

NPM_CLI_PACKAGES = {
    "lark-cli": "@larksuite/cli",
    "codex": "@openai/codex",
    "claude": "@anthropic-ai/claude-code",
    "gemini": "@google/gemini-cli",
    "qwen": "@qwen-code/qwen-code",
    "opencode": "opencode-ai",
}

AI_CLI_COMMANDS = (
    "codex",
    "claude",
    "cursor-agent",
    "gemini",
    "deepseek",
    "qwen",
    "opencode",
)

def print_step(message: str) -> None:
    """Print a setup step in a consistent, easy-to-scan format."""

    print(f"[setup] {message}")


def print_ok(message: str) -> None:
    """Print a successful setup check."""

    print(f"[ok] {message}")


def print_warn(message: str) -> None:
    """Print a non-fatal setup warning."""

    print(f"[warn] {message}")


def print_error(message: str) -> None:
    """Print a setup error that should be fixed before running Autofix."""

    print(f"[error] {message}")


def run_command(args: list[str], *, dry_run: bool = False, check: bool = True) -> subprocess.CompletedProcess[str] | None:
    """Run one installer/check command and echo it before execution.

    All commands are passed as argv lists instead of shell strings. This avoids
    quoting surprises and makes the script safe to read before running.
    """

    print_step("$ " + " ".join(args))
    if dry_run:
        return None
    return subprocess.run(args, check=check, text=True)


def command_path(command: str) -> str:
    """Return the executable path for a command found on PATH."""

    found = shutil.which(command)
    if found:
        return found
    return ""


def command_exists(command: str) -> bool:
    """Return whether a runtime command is currently available."""

    return bool(command_path(command))


def check_python_version() -> bool:
    """Check the Python version needed by the Autofix source files.

    Autofix uses Python 3.9-compatible modern type annotations in its source.
    """

    current = sys.version_info[:3]
    if current < MIN_PYTHON:
        print_error(
            "Python 3.9+ is required, current is "
            f"{current[0]}.{current[1]}.{current[2]} at {sys.executable}"
        )
        return False
    print_ok(f"Python {current[0]}.{current[1]}.{current[2]}: {sys.executable}")
    return True


def ensure_pip(dry_run: bool) -> bool:
    """Ensure pip is available for the current Python interpreter."""

    result = subprocess.run([sys.executable, "-m", "pip", "--version"], capture_output=True, text=True)
    if result.returncode == 0:
        print_ok(result.stdout.strip())
        return True
    print_warn("pip is not available; trying ensurepip.")
    try:
        run_command([sys.executable, "-m", "ensurepip", "--upgrade"], dry_run=dry_run)
    except subprocess.CalledProcessError as exc:
        print_error(f"ensurepip failed with exit code {exc.returncode}")
        return False
    return True


def install_python_requirements(dry_run: bool) -> bool:
    """Install Python dependencies from requirements.txt."""

    if not REQUIREMENTS_FILE.exists():
        print_error(f"Missing requirements file: {REQUIREMENTS_FILE}")
        return False
    if not ensure_pip(dry_run):
        return False
    try:
        run_command([sys.executable, "-m", "pip", "install", "-r", str(REQUIREMENTS_FILE)], dry_run=dry_run)
    except subprocess.CalledProcessError as exc:
        print_error(f"pip install failed with exit code {exc.returncode}")
        return False
    print_ok("Python requirements installed")
    return True


def install_with_brew(command: str, dry_run: bool) -> bool:
    """Install a command with Homebrew when a formula is known."""

    package = BREW_PACKAGES.get(command)
    if not package or not command_exists("brew"):
        return False
    try:
        run_command(["brew", "install", package], dry_run=dry_run)
    except subprocess.CalledProcessError as exc:
        print_warn(f"brew install {package} failed with exit code {exc.returncode}")
        return False
    return True


def install_with_apt(command: str, dry_run: bool) -> bool:
    """Install a command with apt-get when available.

    The command uses sudo because system package installation normally needs it.
    On machines without sudo or apt-get this function simply returns False.
    """

    package = APT_PACKAGES.get(command)
    if not package or not command_exists("apt-get") or not command_exists("sudo"):
        return False
    try:
        run_command(["sudo", "apt-get", "update"], dry_run=dry_run)
        run_command(["sudo", "apt-get", "install", "-y", package], dry_run=dry_run)
    except subprocess.CalledProcessError as exc:
        print_warn(f"apt-get install {package} failed with exit code {exc.returncode}")
        return False
    return True


def ensure_node_and_npm(dry_run: bool) -> bool:
    """Ensure npm exists before installing Node-based CLIs."""

    if command_exists("npm"):
        print_ok(f"npm: {command_path('npm')}")
        return True
    print_warn("npm is missing; trying to install Node.js.")
    system = platform.system().lower()
    if system == "darwin" and install_with_brew("node", dry_run):
        return dry_run or command_exists("npm")
    if system == "linux":
        install_with_apt("node", dry_run)
        install_with_apt("npm", dry_run)
        return dry_run or command_exists("npm")
    return False


def install_npm_cli(command: str, dry_run: bool) -> bool:
    """Install a known Node-based CLI package."""

    package = NPM_CLI_PACKAGES.get(command)
    if not package:
        return False
    if not ensure_node_and_npm(dry_run):
        print_warn(f"Cannot install {command}: npm is unavailable.")
        return False
    try:
        run_command(["npm", "install", "-g", package], dry_run=dry_run)
    except subprocess.CalledProcessError as exc:
        print_warn(f"npm install -g {package} failed with exit code {exc.returncode}")
        return False
    return True


def ensure_command(command: str, *, required: bool, dry_run: bool) -> bool:
    """Check one external command and install it when the source is known."""

    if command_exists(command):
        print_ok(f"{command}: {command_path(command)}")
        return True

    print_warn(f"{command} is missing. {REQUIRED_COMMANDS.get(command, '')}".strip())
    installed = False
    if command in {"git", "rg"}:
        system = platform.system().lower()
        if system == "darwin":
            installed = install_with_brew(command, dry_run)
        elif system == "linux":
            installed = install_with_apt(command, dry_run)
    elif command == "lark-cli":
        installed = install_npm_cli(command, dry_run)

    if dry_run and installed:
        return True
    if command_exists(command):
        print_ok(f"{command}: {command_path(command)}")
        return True
    if required:
        print_error(f"{command} is still missing.")
    else:
        print_warn(f"{command} is still missing.")
    return False


def ensure_required_commands(dry_run: bool) -> bool:
    """Install or check runtime commands that the code calls directly."""

    ok = True
    for command in REQUIRED_COMMANDS:
        ok = ensure_command(command, required=True, dry_run=dry_run) and ok
    return ok


def installed_ai_clis() -> list[str]:
    """Return all editable AI CLI commands currently visible to Autofix."""

    return [command for command in AI_CLI_COMMANDS if command_exists(command)]


def ensure_ai_cli(choice: str, dry_run: bool) -> bool:
    """Ensure an editable AI CLI is available for automatic repair.

    The management console can bind projects to different CLI backends. Setup
    only installs the requested/default CLI; it does not force every optional AI
    CLI onto the machine.
    """

    current = installed_ai_clis()
    if choice == "none":
        if current:
            print_ok("AI CLI available: " + ", ".join(current))
        else:
            print_warn("No AI CLI found. Configure one manually before running repair tasks.")
        return True

    if choice == "auto":
        if current:
            print_ok("AI CLI available: " + ", ".join(current))
            return True
        choice = "codex"
        print_warn("No AI CLI found; installing Codex CLI as the default editable CLI.")

    if command_exists(choice):
        print_ok(f"{choice}: {command_path(choice)}")
        return True

    if not install_npm_cli(choice, dry_run):
        print_error(
            f"Cannot auto-install AI CLI '{choice}'. Install it manually or use "
            "--ai-cli codex/claude/cursor-agent/gemini/deepseek/qwen/opencode/none."
        )
        return False

    if dry_run:
        return True
    if command_exists(choice):
        print_ok(f"{choice}: {command_path(choice)}")
        return True
    print_error(f"AI CLI '{choice}' is still missing after install.")
    return False


def check_local_imports() -> bool:
    """Verify the local tools directory exists before setup reports success."""

    if not TOOLS_DIR.exists():
        print_error(f"Missing tools directory: {TOOLS_DIR}")
        return False
    print_ok(f"Autofix tools directory: {TOOLS_DIR}")
    return True


def check_project_skill_files() -> bool:
    """Print the path of optional project-local skill files."""

    if not PAGE_POLISH_SKILL_FILE.exists():
        print_warn(f"Page polish skill file is missing: {PAGE_POLISH_SKILL_FILE}")
        return False
    print_ok(f"Page polish skill: {PAGE_POLISH_SKILL_FILE}")
    print_step(
        "Harness prompt: 请读取 Autofix/skills/html-page-polish-skill.md，并在功能不变的前提下优化页面的实用性和美观性。"
    )
    return True


def build_parser() -> argparse.ArgumentParser:
    """Create the command-line interface for one-click setup."""

    parser = argparse.ArgumentParser(description="Install Autofix code dependencies.")
    parser.add_argument("--dry-run", action="store_true", help="Print install commands without running them.")
    parser.add_argument(
        "--ai-cli",
        default="auto",
        choices=("auto", "none", *AI_CLI_COMMANDS),
        help=(
            "Editable AI CLI to ensure. 'auto' keeps an existing AI CLI or installs codex; "
            "'none' only checks and prints status."
        ),
    )
    return parser


def main() -> int:
    """Run setup and return a process exit code."""

    args = build_parser().parse_args()
    print_step(f"Autofix directory: {APP_DIR}")

    ok = True
    ok = check_python_version() and ok
    ok = check_local_imports() and ok
    ok = check_project_skill_files() and ok
    ok = install_python_requirements(args.dry_run) and ok
    ok = ensure_required_commands(args.dry_run) and ok
    ok = ensure_ai_cli(args.ai_cli, args.dry_run) and ok

    if ok:
        print_ok("Setup finished. Start with: python3 Autofix/tools/autofix_manager.py")
        return 0
    print_error("Setup finished with missing dependencies. Fix the errors above and run again.")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
