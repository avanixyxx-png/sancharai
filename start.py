"""
One-command launcher for SancharAI.

Does the whole dance that otherwise has to be done by hand every single time:

    1. stops any SancharAI already running on the port
    2. starts an ngrok tunnel (if ngrok is set up)
    3. reads the public URL out of ngrok's local API and writes it into .env
    4. starts the web server
    5. shuts both down cleanly on Ctrl+C

Step 3 is the one worth automating: the free ngrok URL changes every restart,
so doing it manually means editing .env and restarting on every single run.

Run it with:   start.bat        (or:  py start.py)
"""
from __future__ import annotations

import contextlib
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

import httpx

# The Windows console defaults to cp1252, which cannot encode box-drawing or
# Indic characters - printing one raises UnicodeEncodeError and kills the
# launcher before it starts anything. app.py does the same thing for the same
# reason.
for _stream in (sys.stdout, sys.stderr):
    with contextlib.suppress(Exception):
        _stream.reconfigure(encoding="utf-8", errors="replace")

HERE = Path(__file__).parent
ENV_FILE = HERE / ".env"
PORT = int(os.getenv("PORT", "8010"))
NGROK_API = "http://127.0.0.1:4040/api/tunnels"

# Windows terminals handle these fine; they make the output scannable.
OK, WARN, BAD, DIM, BOLD, END = "\033[32m", "\033[33m", "\033[31m", "\033[90m", "\033[1m", "\033[0m"


def say(msg: str, colour: str = "") -> None:
    print(f"{colour}{msg}{END}", flush=True)


def rule() -> None:
    # Plain ASCII: safe on every Windows console regardless of code page.
    say("-" * 62, DIM)


# --------------------------------------------------------------- housekeeping
def stop_existing() -> None:
    """Kill a previous run so the port and the database lock are free.

    Only targets processes running THIS project's run.py - other Python work
    on the machine is left alone.
    """
    try:
        out = subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             "Get-CimInstance Win32_Process -Filter \"Name='python.exe'\" | "
             "Where-Object { $_.CommandLine -match 'run\\.py' } | "
             "ForEach-Object { $_.ProcessId }"],
            capture_output=True, text=True, timeout=20,
        )
        pids = [p.strip() for p in out.stdout.split() if p.strip().isdigit()]
        for pid in pids:
            subprocess.run(["taskkill", "/F", "/PID", pid],
                           capture_output=True, timeout=10)
        if pids:
            say(f"  stopped {len(pids)} previous server process(es)", DIM)
            time.sleep(1.5)
    except Exception:
        pass  # never let cleanup stop a launch

    lock = HERE / ".scheduler.lock"
    if lock.exists():
        try:
            lock.unlink()
        except OSError:
            pass


def ngrok_ready() -> tuple[bool, str]:
    """Is ngrok installed AND has an authtoken? Both are needed to open a tunnel."""
    if not shutil.which("ngrok"):
        return False, "ngrok is not installed"
    try:
        res = subprocess.run(["ngrok", "config", "check"],
                             capture_output=True, text=True, timeout=15)
    except Exception as exc:
        return False, str(exc)
    if res.returncode != 0 or "ERROR" in (res.stdout + res.stderr).upper():
        return False, "ngrok has no authtoken yet"
    return True, ""


def start_ngrok() -> tuple[subprocess.Popen | None, str]:
    """Launch the tunnel and return (process, public_https_url)."""
    proc = subprocess.Popen(
        ["ngrok", "http", str(PORT), "--log", "stdout"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    # ngrok exposes a local API listing its tunnels once it's up. Polling that
    # is far more reliable than scraping its console output.
    for _ in range(40):
        time.sleep(0.5)
        try:
            tunnels = httpx.get(NGROK_API, timeout=2).json().get("tunnels", [])
        except Exception:
            continue
        for t in tunnels:
            url = t.get("public_url", "")
            if url.startswith("https://"):
                return proc, url
    proc.terminate()
    return None, ""


def write_public_url(url: str) -> None:
    """Set PUBLIC_BASE_URL in .env, preserving every other line."""
    lines = ENV_FILE.read_text(encoding="utf-8").splitlines() if ENV_FILE.exists() else []
    replaced = False
    for i, line in enumerate(lines):
        if re.match(r"\s*PUBLIC_BASE_URL\s*=", line):
            lines[i] = f"PUBLIC_BASE_URL={url}"
            replaced = True
            break
    if not replaced:
        lines.append(f"PUBLIC_BASE_URL={url}")
    ENV_FILE.write_text("\n".join(lines) + "\n", encoding="utf-8")


# ---------------------------------------------------------------------- main
def main() -> int:
    os.chdir(HERE)
    rule()
    say("  SancharAI", BOLD)
    rule()

    say("\n[1/3] Cleaning up any previous run…", BOLD)
    stop_existing()

    say("\n[2/3] Opening a public tunnel…", BOLD)
    tunnel = None
    ready, why = ngrok_ready()
    if ready:
        tunnel, public_url = start_ngrok()
        if public_url:
            write_public_url(public_url)
            say(f"  {public_url}", OK)
            say("  saved to .env — Twilio can now reach this machine", DIM)
        else:
            say("  ngrok started but no tunnel appeared. Phone calls will not work.", BAD)
    else:
        say(f"  skipped: {why}", WARN)
        say("", DIM)
        say("  The dashboard will still work, but PHONE CALLS WILL NOT,", WARN)
        say("  because Twilio has no way to reach your computer.", WARN)
        say("", DIM)
        say("  To fix (one time only):", BOLD)
        say("    1. Make a free account at  https://dashboard.ngrok.com/signup")
        say("    2. Copy your authtoken from the page it shows you")
        say("    3. Run:   ngrok config add-authtoken YOUR_TOKEN_HERE")
        say("    4. Run this launcher again")

    say(f"\n[3/3] Starting the server on port {PORT}…", BOLD)
    rule()
    say(f"  Open:  http://127.0.0.1:{PORT}", OK + BOLD)
    say("  Press Ctrl+C here to stop everything.", DIM)
    rule()
    print()

    env = {**os.environ, "PORT": str(PORT)}
    # Plain HTTP locally, so the session cookie must not be Secure-only or you
    # could never stay logged in.
    env.setdefault("SANCHARAI_SECURE_COOKIES", "0")

    server = subprocess.Popen([sys.executable, "run.py"], env=env)
    try:
        server.wait()
    except KeyboardInterrupt:
        say("\n\nShutting down…", BOLD)
    finally:
        for proc, name in ((server, "server"), (tunnel, "tunnel")):
            if proc and proc.poll() is None:
                try:
                    proc.terminate()
                    proc.wait(timeout=5)
                except Exception:
                    with_kill = getattr(proc, "kill", None)
                    if with_kill:
                        with_kill()
                say(f"  {name} stopped", DIM)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(0)
