"""Claude Code hook: records each session's state for the widget.

Claude Code runs this for every hook event with the event JSON on stdin.
It must stay fast and must never fail loudly, so it is stdlib only and
swallows every error.
"""
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dots import host, store  # noqa: E402

# Tools that stop and wait for you to answer.
QUESTION_TOOLS = {"AskUserQuestion", "ExitPlanMode"}
QUESTION_NOTIFICATIONS = {
    "permission_prompt", "elicitation_dialog", "elicitation_url_dialog", "agent_needs_input",
}
# Events where the foreground window is the terminal you are typing in.
REFRESH_HOST_EVENTS = {"SessionStart", "UserPromptSubmit"}


def next_state(event: str, payload: dict, current):
    """The state this event moves the session to, or None to leave it."""
    if event == "SessionStart":
        return current if payload.get("source") == "compact" and current else store.IDLE
    if event == "UserPromptSubmit":
        return store.WORKING
    if event == "PreToolUse":
        return store.QUESTION if payload.get("tool_name") in QUESTION_TOOLS else store.WORKING
    if event in ("PostToolUse", "PostToolUseFailure", "ElicitationResult"):
        return store.WORKING
    if event in ("PermissionRequest", "Elicitation"):
        return store.QUESTION
    if event == "Notification":
        kind = payload.get("notification_type")
        if kind in QUESTION_NOTIFICATIONS:
            return store.QUESTION
        # Fires after ~60s at the prompt; catches turns you interrupted with Esc,
        # which end without a Stop event.
        if kind == "idle_prompt" and current in (store.WORKING, store.QUESTION):
            return store.IDLE
        return None
    if event == "Stop":
        return store.DONE
    if event == "StopFailure":
        return store.ERROR
    return None


def detail_for(event: str, payload: dict):
    if event == "StopFailure":
        kind = payload.get("error_type") or "error"
        msg = payload.get("error_message") or ""
        return f"{kind}: {msg}".strip(": ")
    if event in ("PreToolUse", "PermissionRequest"):
        return payload.get("tool_name")
    if event == "Notification":
        return payload.get("message")
    if event == "Stop":
        text = (payload.get("last_assistant_message") or "").strip().splitlines()
        return text[0][:160] if text else None
    return None


def handle(payload: dict, started: float) -> None:
    event = payload.get("hook_event_name") or ""
    session_id = payload.get("session_id")
    if not session_id:
        return
    path = store.session_path(session_id)

    if event == "SessionEnd":
        store.remove(path)
        store.remove(store.status_path(session_id))
        return

    rec = store.read_json(path, {}) or {}
    # Async hooks can finish out of order; never let an older event win.
    if rec.get("event_at", 0) > started:
        return
    current = rec.get("state")
    state = next_state(event, payload, current)
    if state is None:
        return
    # Subagents (possibly still running in the background after the main turn
    # ended) only matter when they need you, or once you have answered them.
    if payload.get("agent_id") and store.QUESTION not in (state, current):
        return
    if state == current and state in (store.WORKING, store.QUESTION) and rec.get("host"):
        return  # nothing new; skip the write on busy tool loops

    cwd = payload.get("cwd") or rec.get("cwd") or ""
    rec.update({
        "session_id": session_id,
        "cwd": cwd,
        "project": os.path.basename(cwd.rstrip("/\\")) or cwd or "session",
        "transcript_path": payload.get("transcript_path") or rec.get("transcript_path"),
        "event": event,
        "event_at": started,
        "updated_at": time.time(),
    })
    rec.setdefault("created_at", started)
    if state != current or state in (store.DONE, store.ERROR):
        rec["state"] = state
        rec["state_at"] = started
        rec["detail"] = detail_for(event, payload)
    old = rec.get("host") or {}
    # On Windows keep looking for the terminal until one turns up: a background
    # job has none until the terminal that sent it away is known.
    if event in REFRESH_HOST_EVENTS or not old or (host.IS_WINDOWS and not old.get("hwnd")):
        fresh = host.describe_host()
        if not fresh.get("hwnd") and old.get("hwnd"):
            fresh["hwnd"] = old["hwnd"]
        rec["host"] = fresh
    store.write_json(path, rec)


def main() -> None:
    started = time.time()
    try:
        payload = json.loads(sys.stdin.buffer.read().decode("utf-8", "replace"))
        if isinstance(payload, dict):
            handle(payload, started)
    except Exception as exc:  # a broken widget must never break Claude
        try:
            with open(store.home() / "hook-errors.log", "a", encoding="utf-8") as f:
                f.write(f"{time.ctime()} {type(exc).__name__}: {exc}\n")
        except OSError:
            pass
    sys.exit(0)


if __name__ == "__main__":
    main()
