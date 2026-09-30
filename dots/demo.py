"""Preview the widget with fake sessions: python -m dots.demo

Uses a separate data folder (~/.dots-demo) so your real sessions are untouched.
"""
import os
import sys
import time
from pathlib import Path

os.environ["DOTS_HOME"] = str(Path.home() / ".dots-demo")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dots import store  # noqa: E402

SAMPLES = [
    ("api-server", store.WORKING, None),
    ("frontend", store.QUESTION, "Bash"),
    ("infra", store.DONE, "Deployed the staging stack and updated the runbook."),
    ("scraper", store.ERROR, "overloaded: API is temporarily overloaded"),
    ("docs", store.DONE, "Fixed the broken links."),
    ("ml-pipeline", store.IDLE, None),
]


def seed() -> None:
    now = time.time()
    for path in store.sessions_dir().glob("*"):
        store.remove(path)
    seen = {}
    for i, (project, state, detail) in enumerate(SAMPLES):
        sid = f"demo-{i}"
        store.write_json(store.session_path(sid), {
            "session_id": sid, "cwd": f"~/code/{project}", "project": project,
            "state": state, "state_at": now - 60 * i, "created_at": now - 3600 + i,
            "updated_at": now, "detail": detail, "host": {},
        })
        store.write_json(store.status_path(sid), {"context": 12 + 11 * i, "model": "Opus"})
        if project == "docs":
            seen[sid] = now  # read already: steady green
    store.write_json(store.seen_path(), seen)
    store.write_json(store.usage_path(), {
        "five_hour": {"used_percentage": 47.0, "resets_at": now + 2 * 3600 + 13 * 60},
        "seven_day": {"used_percentage": 31.0, "resets_at": now + 3 * 86400 + 4 * 3600},
        "updated_at": now,
    })


if __name__ == "__main__":
    seed()
    from dots.widget import main
    main()
