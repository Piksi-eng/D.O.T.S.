"""Install (or remove) D.O.T.S.

    python install.py               hooks + status line + start on login
    python install.py --no-autostart
    python install.py --uninstall

Run it with the same Python you installed the requirements into.
Your Claude Code settings.json is backed up before it is changed.
"""
import argparse
import json
import os
import shutil
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from dots import autostart, store  # noqa: E402

HOOK = ROOT / "dots" / "hook.py"
STATUSLINE = ROOT / "dots" / "statusline.py"
MARKER = "dots/hook.py"

# event -> run in the background? Tool events fire constantly, so they must not
# slow Claude down; the rest are rare and ordering matters more for them.
EVENTS = {
    "SessionStart": False,
    "SessionEnd": False,
    "UserPromptSubmit": False,
    "PreToolUse": True,
    "PostToolUse": True,
    "PostToolUseFailure": True,
    "PermissionRequest": False,
    "Notification": False,
    "Elicitation": False,
    "ElicitationResult": False,
    "Stop": False,
    "StopFailure": False,
}


def settings_path() -> Path:
    base = os.environ.get("CLAUDE_CONFIG_DIR")
    return (Path(base) if base else Path.home() / ".claude") / "settings.json"


def load_settings(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8") or "{}")
    except ValueError as exc:
        sys.exit(f"Could not parse {path}: {exc}\nFix the file (or move it away) and run again.")


def save_settings(path: Path, settings: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        stamp = time.strftime("%Y%m%d-%H%M%S")
        backup = path.with_name(f"{path.name}.dots-backup-{stamp}")
        n = 1
        while backup.exists():
            n += 1
            backup = path.with_name(f"{path.name}.dots-backup-{stamp}-{n}")
        shutil.copy2(path, backup)
        print(f"  backup: {backup}")
    path.write_text(json.dumps(settings, indent=2) + "\n", encoding="utf-8")


def fwd(path) -> str:
    """Forward slashes: Git Bash on Windows eats backslashes."""
    return str(path).replace("\\", "/")


def is_ours(hook: dict) -> bool:
    text = " ".join([hook.get("command", "")] + list(hook.get("args") or []))
    return MARKER in fwd(text)


def strip_hooks(settings: dict) -> None:
    hooks = settings.get("hooks") or {}
    for event in list(hooks):
        groups = []
        for group in hooks[event] or []:
            kept = [h for h in group.get("hooks", []) if not is_ours(h)]
            if kept:
                groups.append({**group, "hooks": kept})
        if groups:
            hooks[event] = groups
        else:
            del hooks[event]
    if hooks:
        settings["hooks"] = hooks
    else:
        settings.pop("hooks", None)


def add_hooks(settings: dict) -> None:
    hooks = settings.setdefault("hooks", {})
    for event, background in EVENTS.items():
        # Exec form: no shell in between, so paths with spaces just work.
        hook = {"type": "command", "command": fwd(sys.executable), "args": [fwd(HOOK)], "timeout": 10}
        if background:
            hook["async"] = True
        hooks.setdefault(event, []).append({"hooks": [hook]})


def statusline_command() -> str:
    py, script = fwd(sys.executable), fwd(STATUSLINE)
    if sys.platform == "win32":
        bash = shutil.which("bash") or r"C:\Program Files\Git\bin\bash.exe"
        if not os.path.exists(bash):  # Claude Code falls back to PowerShell
            return f'& "{py}" "{script}"'
    return f'"{py}" "{script}"'


def install(with_autostart: bool) -> None:
    try:
        import PySide6  # noqa: F401
    except ImportError:
        print("! PySide6 is not installed for this Python. Run:\n"
              f"    \"{sys.executable}\" -m pip install -r requirements.txt\n")

    path = settings_path()
    print(f"Claude Code settings: {path}")
    settings = load_settings(path)
    strip_hooks(settings)
    add_hooks(settings)

    cfg = store.load_config()
    current = settings.get("statusLine") or {}
    command = current.get("command", "")
    if command and "statusline.py" not in fwd(command):
        cfg["statusline_chain"] = command  # keep showing your existing status line
        print(f"  keeping your status line (wrapped): {command}")
    settings["statusLine"] = {"type": "command", "command": statusline_command()}
    store.save_config(cfg)
    save_settings(path, settings)
    print("  hooks + status line installed")

    if with_autostart:
        autostart.enable()
        print("  starts on login: yes")
    print("\nDone. Restart your Claude Code sessions (hooks load at startup), then run:\n"
          f"    \"{autostart.gui_python()}\" \"{autostart.LAUNCHER}\"")


def uninstall() -> None:
    path = settings_path()
    settings = load_settings(path)
    strip_hooks(settings)
    cfg = store.load_config()
    if "statusline.py" in fwd((settings.get("statusLine") or {}).get("command", "")):
        chain = cfg.pop("statusline_chain", None)
        if chain:
            settings["statusLine"] = {"type": "command", "command": chain}
        else:
            settings.pop("statusLine", None)
        store.save_config(cfg)
    save_settings(path, settings)
    autostart.disable()
    print("Removed D.O.T.S. hooks, status line and autostart. "
          f"Session data is left in {store.home()} (delete it if you like).")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--uninstall", action="store_true", help="remove everything install added")
    parser.add_argument("--no-autostart", action="store_true", help="don't start the widget on login")
    args = parser.parse_args()
    uninstall() if args.uninstall else install(not args.no_autostart)


if __name__ == "__main__":
    main()
