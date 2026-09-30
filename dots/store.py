"""Shared on-disk state. Stdlib only: the hook and status line import this."""
import json
import os
import time
from pathlib import Path

# Session states written by the hook.
IDLE = "idle"          # session open, nothing happened yet
WORKING = "working"    # Claude is busy
QUESTION = "question"  # Claude is waiting on you (permission, question, plan)
DONE = "done"          # Claude finished its turn
ERROR = "error"        # the turn died on an API error


def home() -> Path:
    return Path(os.environ.get("DOTS_HOME") or Path.home() / ".dots")


def sessions_dir() -> Path:
    path = home() / "sessions"
    path.mkdir(parents=True, exist_ok=True)
    return path


def session_path(session_id: str) -> Path:
    safe = "".join(c for c in session_id if c.isalnum() or c in "-_") or "unknown"
    return sessions_dir() / f"{safe}.json"


def status_path(session_id: str) -> Path:
    return session_path(session_id).with_suffix(".status")


def usage_path() -> Path:
    return home() / "usage.json"


def seen_path() -> Path:
    return home() / "seen.json"


def config_path() -> Path:
    return home() / "config.json"


def read_json(path: Path, default=None):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def write_json(path: Path, data) -> None:
    """Atomic write, retried because Windows refuses to replace a file that is open."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=1)
    for _ in range(20):
        try:
            os.replace(tmp, path)
            return
        except PermissionError:
            time.sleep(0.025)
    try:
        os.remove(tmp)
    except OSError:
        pass


def remove(path: Path) -> None:
    try:
        os.remove(path)
    except OSError:
        pass


def load_config() -> dict:
    return read_json(config_path(), {}) or {}


def save_config(cfg: dict) -> None:
    write_json(config_path(), cfg)
