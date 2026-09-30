"""Claude Code status line: saves your usage limits for the widget's meters.

Claude Code passes session data (including `rate_limits` on Pro/Max plans)
to the status line command on every refresh. We store it, then print a
compact status line, or run the status line you had before installing
D.O.T.S. and print its output instead.
"""
import json
import os
import shutil
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dots import store  # noqa: E402

ORANGE = "\033[38;2;255;138;31m"
DIM = "\033[38;2;138;127;118m"
RESET = "\033[0m"


def record(data: dict) -> None:
    limits = data.get("rate_limits") or {}
    windows = {k: v for k, v in limits.items() if isinstance(v, dict) and "used_percentage" in v}
    if windows:
        usage = store.read_json(store.usage_path(), {}) or {}
        usage.update(windows)
        usage["updated_at"] = time.time()
        store.write_json(store.usage_path(), usage)

    session_id = data.get("session_id")
    if session_id and store.session_path(session_id).exists():
        ctx = (data.get("context_window") or {}).get("used_percentage")
        model = (data.get("model") or {}).get("display_name")
        store.write_json(store.status_path(session_id), {"context": ctx, "model": model})


def run_previous(command: str, raw: str):
    """Run the status line that was configured before D.O.T.S., the way Claude Code would."""
    if sys.platform == "win32":
        bash = shutil.which("bash") or r"C:\Program Files\Git\bin\bash.exe"
        argv = [bash, "-c", command] if os.path.exists(bash) else \
            ["powershell", "-NoProfile", "-Command", command]
    else:
        argv = ["sh", "-c", command]
    try:
        return subprocess.run(argv, input=raw, capture_output=True, text=True,
                              encoding="utf-8", timeout=10).stdout
    except (OSError, subprocess.SubprocessError):
        return None


def default_line(data: dict) -> str:
    parts = []
    model = (data.get("model") or {}).get("display_name")
    if model:
        parts.append(f"{ORANGE}\u25c6 {model}{RESET}")
    ctx = (data.get("context_window") or {}).get("used_percentage")
    if ctx is not None:
        parts.append(f"ctx {ctx:.0f}%")
    limits = data.get("rate_limits") or {}
    for key, label in (("five_hour", "5h"), ("seven_day", "wk")):
        pct = (limits.get(key) or {}).get("used_percentage")
        if pct is not None:
            parts.append(f"{label} {pct:.0f}%")
    return f" {DIM}\u00b7{RESET} ".join(parts)


def main() -> None:
    sys.stdout.reconfigure(encoding="utf-8")
    raw = sys.stdin.buffer.read().decode("utf-8", "replace")
    try:
        data = json.loads(raw)
    except ValueError:
        data = {}
    try:
        record(data)
    except Exception:
        pass
    previous = store.load_config().get("statusline_chain")
    out = run_previous(previous, raw) if previous else None
    sys.stdout.write(out if out is not None else default_line(data))


if __name__ == "__main__":
    main()
