"""
start.py — single-command launcher for the Upgrade Agent.

Starts:
  1. FastAPI backend  (port 8000)  — includes Claude Router
  2. Next.js frontend (port 3000)

Usage (from the project root):
    python start.py

Flags:
    --port-api  <n>   FastAPI port  (default 8000)
    --port-web  <n>   Next.js port  (default 3000)
    --no-reload       Disable FastAPI auto-reload
    --streamlit       Use Streamlit UI instead of Next.js

Press Ctrl+C to stop all processes.
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
WEB_DIR = ROOT / "upgrade-web"

# ANSI colour tags for process prefixes
_COLORS = {
    "api":      "\033[36m",   # cyan
    "web":      "\033[35m",   # magenta
    "router":   "\033[33m",   # yellow
    "launcher": "\033[32m",   # green
}
_RESET = "\033[0m"
_BOLD  = "\033[1m"


def _tag(name: str) -> str:
    color = _COLORS.get(name, "")
    return f"{_BOLD}{color}[{name.upper():^8}]{_RESET} "


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _stream(proc: subprocess.Popen, tag: str) -> None:
    """Forward process stdout/stderr to our stdout with a tag prefix."""
    assert proc.stdout is not None
    for line in proc.stdout:
        print(_tag(tag) + line, end="", flush=True)


def _find_npm() -> str:
    npm = shutil.which("npm") or shutil.which("npm.cmd")
    if not npm:
        sys.exit("ERROR: npm not found. Install Node.js 18+ and retry.")
    return npm


def _find_python() -> str:
    return sys.executable


def _ensure_env_local() -> None:
    """Create upgrade-web/.env.local from the example if it doesn't exist."""
    example = WEB_DIR / ".env.local.example"
    target  = WEB_DIR / ".env.local"
    if not target.exists() and example.exists():
        target.write_text(example.read_text(encoding="utf-8"), encoding="utf-8")
        print(_tag("launcher") + f"Created {target} from example.")


def _ensure_npm_install() -> None:
    """Run npm install in upgrade-web if node_modules is missing."""
    if not (WEB_DIR / "node_modules").exists():
        print(_tag("launcher") + "Running npm install in upgrade-web/ ...")
        subprocess.run([_find_npm(), "install"], cwd=str(WEB_DIR), check=True)
        print(_tag("launcher") + "npm install done.")


def _print_router_table() -> None:
    """Print the Claude Router routing table at startup."""
    try:
        from upgrade_lib.claude_router import default_router
        print(_tag("router") + "Claude Router active — routing table:")
        for agent, model in default_router._defaults.items():
            short = model.replace("claude-", "").replace("-4-5-20251001", "").replace("-4-5", "")
            print(_tag("router") + f"  {agent:<10} -> {short}")
    except Exception as exc:
        print(_tag("router") + f"Could not load router: {exc}")


# ---------------------------------------------------------------------------
# Process launchers
# ---------------------------------------------------------------------------

def _start_api(port: int, reload: bool) -> subprocess.Popen:
    cmd = [
        _find_python(), "-m", "uvicorn",
        "upgrade_api.main:app",
        "--host", "0.0.0.0",
        "--port", str(port),
    ]
    if reload:
        cmd.append("--reload")
    env = {**os.environ, "PYTHONUNBUFFERED": "1", "PYTHONIOENCODING": "utf-8"}
    return subprocess.Popen(
        cmd,
        cwd=str(ROOT),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=env,
    )


def _start_web(port: int, api_port: int) -> subprocess.Popen:
    env = {
        **os.environ,
        "NEXT_PUBLIC_API_BASE": f"http://localhost:{api_port}",
        "PORT": str(port),
    }
    cmd = [_find_npm(), "run", "dev", "--", "-p", str(port)]
    return subprocess.Popen(
        cmd,
        cwd=str(WEB_DIR),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=env,
    )


def _start_streamlit() -> subprocess.Popen:
    cmd = [_find_python(), "-m", "streamlit", "run", "upgrade-frontend/app.py"]
    env = {**os.environ, "PYTHONUNBUFFERED": "1", "PYTHONIOENCODING": "utf-8"}
    return subprocess.Popen(
        cmd,
        cwd=str(ROOT),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=env,
    )


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(description="Upgrade Agent launcher")
    parser.add_argument("--port-api",   type=int, default=8000)
    parser.add_argument("--port-web",   type=int, default=3000)
    parser.add_argument("--no-reload",  action="store_true")
    parser.add_argument("--streamlit",  action="store_true",
                        help="Use Streamlit UI instead of Next.js")
    args = parser.parse_args()

    print()
    print(_tag("launcher") + _BOLD + "Upgrade Agent — starting all services" + _RESET)
    print(_tag("launcher") + f"  FastAPI  -> http://localhost:{args.port_api}")
    if args.streamlit:
        print(_tag("launcher") + f"  Streamlit -> http://localhost:8501")
    else:
        print(_tag("launcher") + f"  Next.js  -> http://localhost:{args.port_web}")
    print(_tag("launcher") + "  Claude Router is embedded in FastAPI (no separate process)")
    print()

    # Pre-flight checks
    _print_router_table()
    print()

    if not args.streamlit:
        _ensure_env_local()
        _ensure_npm_install()

    # Launch processes
    procs: list[tuple[subprocess.Popen, str]] = []

    print(_tag("launcher") + "Starting FastAPI backend...")
    api_proc = _start_api(args.port_api, reload=not args.no_reload)
    procs.append((api_proc, "api"))
    time.sleep(1)  # brief head start for the API

    if args.streamlit:
        print(_tag("launcher") + "Starting Streamlit UI...")
        web_proc = _start_streamlit()
        procs.append((web_proc, "web"))
    else:
        print(_tag("launcher") + "Starting Next.js frontend...")
        web_proc = _start_web(args.port_web, args.port_api)
        procs.append((web_proc, "web"))

    # Start a reader thread per process
    threads = []
    for proc, tag in procs:
        t = threading.Thread(target=_stream, args=(proc, tag), daemon=True)
        t.start()
        threads.append(t)

    print()
    print(_tag("launcher") + _BOLD + "All services running. Press Ctrl+C to stop." + _RESET)
    print()

    try:
        # Wait for any process to exit unexpectedly
        while True:
            for proc, tag in procs:
                if proc.poll() is not None:
                    print()
                    print(_tag("launcher") + f"WARNING: {tag.upper()} process exited (code {proc.returncode}). Shutting down.")
                    raise KeyboardInterrupt
            time.sleep(1)
    except KeyboardInterrupt:
        print()
        print(_tag("launcher") + "Stopping all services...")
        for proc, tag in procs:
            try:
                proc.terminate()
                proc.wait(timeout=5)
            except Exception:
                proc.kill()
            print(_tag("launcher") + f"  {tag.upper()} stopped.")
        print(_tag("launcher") + "Done.")


if __name__ == "__main__":
    main()
