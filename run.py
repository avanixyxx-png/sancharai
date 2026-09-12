"""
Development launcher.

    python run.py                 # port 8000, or $PORT
    PORT=8010 python run.py       # Windows cmd: set PORT=8010 && python run.py

Deliberately single-worker. The scheduler must run in exactly one process, and
`--reload` spawns a child that would otherwise start a second one and dial every
contact twice. For production use a real process manager and set
SANCHARAI_SCHEDULER=0 on every worker except one.
"""
import os
import socket

import uvicorn

DEFAULT_PORT = 8000


def port_is_free(port: int, host: str = "127.0.0.1") -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        # SO_REUSEADDR would make a busy port look free on some platforms, so
        # this deliberately does a plain bind.
        try:
            sock.bind((host, port))
            return True
        except OSError:
            return False


def first_free_port(start: int, tries: int = 40) -> int | None:
    return next((p for p in range(start, start + tries) if port_is_free(p)), None)


if __name__ == "__main__":
    requested = int(os.getenv("PORT", DEFAULT_PORT))

    if not port_is_free(requested):
        # Raw uvicorn just prints "[Errno 10048] error while attempting to bind",
        # which doesn't say what to do about it. Suggest a port that works.
        suggestion = first_free_port(requested + 1)
        print(f"\n  Port {requested} is already in use by another program.\n")
        if suggestion:
            print("  Start on a free port instead:\n")
            print(f"    Windows (cmd):  set PORT={suggestion} && python run.py")
            print(f"    PowerShell:     $env:PORT={suggestion}; python run.py")
            print(f"    macOS/Linux:    PORT={suggestion} python run.py\n")
            print("  To see what is holding the port:\n")
            print(f"    Windows:        netstat -ano | findstr :{requested}")
            print(f"    macOS/Linux:    lsof -i :{requested}\n")
        else:
            print(f"  No free port found between {requested} and {requested + 40}.\n")
        raise SystemExit(1)

    print(f"\n  SancharAI -> http://127.0.0.1:{requested}\n")
    uvicorn.run("app:app", host="0.0.0.0", port=requested, reload=False, workers=1)
