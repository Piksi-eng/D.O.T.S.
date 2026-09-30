"""Start the widget when you log in (Windows, macOS, Linux)."""
import os
import sys
from pathlib import Path

APP_NAME = "DOTS"
ROOT = Path(__file__).resolve().parent.parent
LAUNCHER = ROOT / "dots_widget.pyw"
_RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
_DESKTOP_FILE = Path.home() / ".config" / "autostart" / "dots.desktop"
_PLIST = Path.home() / "Library" / "LaunchAgents" / "com.dots.widget.plist"


def gui_python() -> str:
    """pythonw.exe on Windows so no console window opens at login."""
    exe = Path(sys.executable)
    if sys.platform == "win32":
        pythonw = exe.with_name("pythonw.exe")
        if pythonw.exists():
            return str(pythonw)
    return str(exe)


def command() -> list:
    return [gui_python(), str(LAUNCHER)]


def is_enabled() -> bool:
    if sys.platform == "win32":
        import winreg
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _RUN_KEY) as key:
                winreg.QueryValueEx(key, APP_NAME)
            return True
        except OSError:
            return False
    if sys.platform == "darwin":
        return _PLIST.exists()
    return _DESKTOP_FILE.exists()


def enable() -> None:
    cmd = command()
    if sys.platform == "win32":
        import winreg
        with winreg.CreateKey(winreg.HKEY_CURRENT_USER, _RUN_KEY) as key:
            winreg.SetValueEx(key, APP_NAME, 0, winreg.REG_SZ, " ".join(f'"{c}"' for c in cmd))
    elif sys.platform == "darwin":
        _PLIST.parent.mkdir(parents=True, exist_ok=True)
        args = "".join(f"<string>{c}</string>" for c in cmd)
        _PLIST.write_text(
            '<?xml version="1.0" encoding="UTF-8"?>\n'
            '<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" '
            '"http://www.apple.com/DTDs/PropertyList-1.0.dtd">\n'
            '<plist version="1.0"><dict>'
            '<key>Label</key><string>com.dots.widget</string>'
            f'<key>ProgramArguments</key><array>{args}</array>'
            '<key>RunAtLoad</key><true/>'
            '</dict></plist>\n', encoding="utf-8")
    else:
        _DESKTOP_FILE.parent.mkdir(parents=True, exist_ok=True)
        _DESKTOP_FILE.write_text(
            "[Desktop Entry]\nType=Application\nName=D.O.T.S.\n"
            f"Exec={' '.join(repr(c) if ' ' in c else c for c in cmd)}\n"
            "X-GNOME-Autostart-enabled=true\n", encoding="utf-8")


def disable() -> None:
    if sys.platform == "win32":
        import winreg
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _RUN_KEY, 0, winreg.KEY_SET_VALUE) as key:
                winreg.DeleteValue(key, APP_NAME)
        except OSError:
            pass
    else:
        path = _PLIST if sys.platform == "darwin" else _DESKTOP_FILE
        try:
            os.remove(path)
        except OSError:
            pass


def set_enabled(on: bool) -> None:
    enable() if on else disable()
