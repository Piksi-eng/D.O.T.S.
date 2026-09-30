"""Find, check and focus the terminal window a Claude session lives in.

Stdlib only (ctypes on Windows) so the hook stays fast and dependency free.
Everything here is best effort: any failure returns None/False.
"""
import json
import os
import subprocess
import sys

IS_WINDOWS = sys.platform == "win32"
# Ancestors whose windows are never the terminal (see find_window).
_NOT_TERMINALS = {"explorer"}

if IS_WINDOWS:
    import ctypes
    from ctypes import wintypes

    _user32 = ctypes.WinDLL("user32", use_last_error=True)
    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

    class _PROCESSENTRY32W(ctypes.Structure):
        _fields_ = [
            ("dwSize", wintypes.DWORD),
            ("cntUsage", wintypes.DWORD),
            ("th32ProcessID", wintypes.DWORD),
            ("th32DefaultHeapID", ctypes.c_void_p),
            ("th32ModuleID", wintypes.DWORD),
            ("cntThreads", wintypes.DWORD),
            ("th32ParentProcessID", wintypes.DWORD),
            ("pcPriClassBase", ctypes.c_long),
            ("dwFlags", wintypes.DWORD),
            ("szExeFile", ctypes.c_wchar * 260),
        ]

    _kernel32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    _kernel32.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
    _kernel32.Process32FirstW.argtypes = [wintypes.HANDLE, ctypes.POINTER(_PROCESSENTRY32W)]
    _kernel32.Process32NextW.argtypes = [wintypes.HANDLE, ctypes.POINTER(_PROCESSENTRY32W)]
    _kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    _kernel32.GetConsoleWindow.restype = wintypes.HWND
    _kernel32.AttachConsole.argtypes = [wintypes.DWORD]

    _EnumWindowsProc = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    _user32.EnumWindows.argtypes = [_EnumWindowsProc, wintypes.LPARAM]
    _user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
    _user32.IsWindowVisible.argtypes = [wintypes.HWND]
    _user32.IsWindow.argtypes = [wintypes.HWND]
    _user32.IsIconic.argtypes = [wintypes.HWND]
    _user32.GetWindow.restype = wintypes.HWND
    _user32.GetWindow.argtypes = [wintypes.HWND, wintypes.UINT]
    _user32.GetAncestor.restype = wintypes.HWND
    _user32.GetAncestor.argtypes = [wintypes.HWND, wintypes.UINT]
    _user32.GetForegroundWindow.restype = wintypes.HWND
    _user32.SetForegroundWindow.argtypes = [wintypes.HWND]
    _user32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
    _user32.GetWindowTextLengthW.argtypes = [wintypes.HWND]

    _TH32CS_SNAPPROCESS = 0x2
    _GW_OWNER = 4
    _GA_ROOTOWNER = 3
    _SW_MINIMIZE = 6
    _SW_RESTORE = 9
    _VK_MENU = 0x12
    _KEYEVENTF_KEYUP = 0x2


def _process_table() -> dict:
    """pid -> (parent_pid, exe_name)."""
    table = {}
    if IS_WINDOWS:
        snap = _kernel32.CreateToolhelp32Snapshot(_TH32CS_SNAPPROCESS, 0)
        if not snap or snap == wintypes.HANDLE(-1).value:
            return table
        try:
            entry = _PROCESSENTRY32W()
            entry.dwSize = ctypes.sizeof(entry)
            ok = _kernel32.Process32FirstW(snap, ctypes.byref(entry))
            while ok:
                table[entry.th32ProcessID] = (entry.th32ParentProcessID, entry.szExeFile)
                ok = _kernel32.Process32NextW(snap, ctypes.byref(entry))
        finally:
            _kernel32.CloseHandle(snap)
    return table


def _posix_parent(pid: int):
    try:
        with open(f"/proc/{pid}/stat", encoding="utf-8") as f:
            stat = f.read()
        name = stat[stat.index("(") + 1:stat.rindex(")")]
        ppid = int(stat[stat.rindex(")") + 2:].split()[1])
        return ppid, name
    except (OSError, ValueError, IndexError):
        pass
    try:  # macOS has no /proc
        out = subprocess.run(["ps", "-o", "ppid=,comm=", "-p", str(pid)],
                             capture_output=True, text=True, timeout=2).stdout.strip()
        ppid, _, name = out.partition(" ")
        return int(ppid), os.path.basename(name.strip())
    except (OSError, ValueError, subprocess.SubprocessError):
        return None


def ancestors(pid: int = None) -> list:
    """[(pid, name), ...] from the given process up to the root."""
    pid = pid or os.getpid()
    chain, seen = [], set()
    table = _process_table() if IS_WINDOWS else None
    while pid and pid not in seen and len(chain) < 64:
        seen.add(pid)
        info = table.get(pid) if IS_WINDOWS else _posix_parent(pid)
        if not info:
            break
        ppid, name = info
        chain.append((pid, name))
        pid = ppid
    return chain


def _base(name: str) -> str:
    name = name.lower()
    return name[:-4] if name.endswith(".exe") else name


def find_claude(chain: list):
    """The Claude Code process among our ancestors (native build first, then node/bun)."""
    for pid, name in chain[1:]:
        if _base(name).startswith("claude"):
            return pid, name
    for pid, name in chain[1:]:
        if _base(name) in ("node", "bun"):
            return pid, name
    return None


def _top_windows() -> dict:
    """pid -> [hwnd, ...] for visible, unowned, titled top-level windows."""
    found = {}

    def callback(hwnd, _):
        if (_user32.IsWindowVisible(hwnd) and not _user32.GetWindow(hwnd, _GW_OWNER)
                and _user32.GetWindowTextLengthW(hwnd) > 0):
            pid = wintypes.DWORD()
            _user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            found.setdefault(pid.value, []).append(hwnd)
        return True

    _user32.EnumWindows(_EnumWindowsProc(callback), 0)
    return found


def _window_pid(hwnd) -> int:
    pid = wintypes.DWORD()
    _user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    return pid.value


def _console_host(console):
    """The visible window showing a console: the terminal that owns its pseudo
    console (Windows Terminal, VS Code), or the classic console window itself."""
    if not console:
        return None
    root = _user32.GetAncestor(console, _GA_ROOTOWNER)
    if root and root != console and _user32.IsWindowVisible(root):
        return int(root)
    if _user32.IsWindowVisible(console):
        return int(console)
    return None


def _console_of(pid: int):
    """Another process's console window, asked for by briefly attaching to it."""
    _kernel32.FreeConsole()
    if not _kernel32.AttachConsole(pid):
        return None
    try:
        return _kernel32.GetConsoleWindow()
    finally:
        _kernel32.FreeConsole()


def _claude_console(claude_pid: int = None):
    """Claude's console window: ours when we share it. Hooks run without a console
    of their own, so otherwise ask Claude's."""
    console = _kernel32.GetConsoleWindow()
    if console or not claude_pid:
        return console
    return _console_of(claude_pid)


def _claude_sessions_dir() -> str:
    base = os.environ.get("CLAUDE_CONFIG_DIR") or os.path.join(os.path.expanduser("~"), ".claude")
    return os.path.join(base, "sessions")


def claude_process(pid) -> dict:
    """What Claude Code says one of its processes is doing, from the file it keeps
    for it: session id, "kind" ("bg" under its daemon), background job id..."""
    if not pid:
        return {}
    try:
        with open(os.path.join(_claude_sessions_dir(), f"{pid}.json"), encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _parked_terminal(job_id):
    """The terminal a background job was sent away from, which goes on
    showing it: the one whose Claude lists the job as parked."""
    if not (IS_WINDOWS and job_id):
        return None
    try:
        names = os.listdir(_claude_sessions_dir())
    except OSError:
        return None
    for name in names:
        stem, ext = os.path.splitext(name)
        if ext == ".json" and stem.isdigit() and claude_process(stem).get("parkedJobId") == job_id:
            found = _console_host(_console_of(int(stem)))
            if found:
                return found
    return None


def find_window(chain: list, console=None):
    """The top-level window hosting this process tree (Windows only)."""
    if not IS_WINDOWS:
        return None
    # The shell is an ancestor when a terminal is started from Explorer (typing
    # "cmd" in the address bar, "Open in Terminal"), but its windows never host us.
    pids = [pid for pid, name in chain if _base(name) not in _NOT_TERMINALS]
    # 1. The foreground window, if it belongs to our tree: at SessionStart and
    #    UserPromptSubmit that is the terminal you just typed in.
    fg = _user32.GetForegroundWindow()
    if fg and _window_pid(fg) in pids:
        return int(fg)
    # 2. The window showing Claude's console: the terminal that owns its pseudo
    #    console (Windows Terminal, VS Code), or the classic console window. With
    #    Windows Terminal as the default terminal it hosts the session without
    #    being one of our ancestors.
    found = _console_host(console)
    if found:
        return found
    # 3. The nearest ancestor that owns a window.
    windows = _top_windows()
    for pid in pids:
        if windows.get(pid):
            return int(windows[pid][0])
    return None


def describe_host() -> dict:
    """Snapshot of where the calling (hook) process runs."""
    chain = ancestors()
    info = {}
    claude = find_claude(chain)
    proc = {}
    if claude:
        info["claude_pid"], info["claude_name"] = claude
        proc = claude_process(claude[0])
        if proc.get("kind") == "bg":
            info["background"] = True  # runs under Claude's daemon, not in a terminal
    try:  # never let window lookup break the hook
        console = _claude_console(info.get("claude_pid")) if IS_WINDOWS else None
        if info.get("background"):
            # Its console is hidden and its ancestors are the daemon's, so look
            # for the terminal that sent it to the background instead.
            hwnd = _parked_terminal(proc.get("jobId"))
        else:
            hwnd = find_window(chain, console)
        if console:
            info["console"] = int(console)
            owner = _user32.GetWindow(console, _GW_OWNER)
            if owner:  # the terminal window showing this pseudo console
                info["console_owner"] = int(owner)
    except Exception:
        hwnd = None
    if hwnd:
        info["hwnd"] = hwnd
    if os.environ.get("WINDOWID"):
        info["window_id"] = os.environ["WINDOWID"]
    term = os.environ.get("TERM_PROGRAM") or ("Windows Terminal" if os.environ.get("WT_SESSION") else None)
    if term:
        info["terminal"] = term
    return info


def foreground_window():
    if IS_WINDOWS:
        return int(_user32.GetForegroundWindow() or 0) or None
    return None


def window_exists(hwnd) -> bool:
    return bool(IS_WINDOWS and hwnd and _user32.IsWindow(hwnd))


def terminal_closed(host: dict) -> bool:
    """True once the window showing a session is gone. Windows Terminal can close
    a window and leave the Claude inside it running with no window at all, so a
    live Claude process does not mean the terminal is still open."""
    if not IS_WINDOWS or host.get("background"):  # background jobs outlive terminals
        return False
    console = host.get("console")
    if console:
        if not window_exists(console):
            return True
        # A pseudo console is owned by the terminal window showing it and is
        # left without an owner when that window closes.
        return bool(host.get("console_owner")) and not window_exists(_user32.GetWindow(console, _GW_OWNER))
    hwnd = host.get("hwnd")
    return bool(hwnd) and not window_exists(hwnd)


def terminal_window(host: dict):
    """The window showing a session right now. Ask the console first: a Windows
    Terminal tab dragged to another window leaves the recorded window stale."""
    console = host.get("console")
    if window_exists(console):
        found = _console_host(console)
        if found:
            return found
    hwnd = host.get("hwnd")
    return hwnd if window_exists(hwnd) else None


def minimize(hwnd) -> None:
    """Minimize a window; Windows then activates the next one in line."""
    if window_exists(hwnd):
        _user32.ShowWindow(hwnd, _SW_MINIMIZE)


def focus(host: dict) -> bool:
    """Bring a session's terminal to the front."""
    hwnd = terminal_window(host)
    if hwnd:
        if _user32.IsIconic(hwnd):
            _user32.ShowWindow(hwnd, _SW_RESTORE)
        if _user32.SetForegroundWindow(hwnd):
            return True
        # Windows only lets the foreground process hand focus away; a synthetic
        # Alt tap satisfies that rule.
        _user32.keybd_event(_VK_MENU, 0, 0, 0)
        _user32.keybd_event(_VK_MENU, 0, _KEYEVENTF_KEYUP, 0)
        return bool(_user32.SetForegroundWindow(hwnd))
    window_id = host.get("window_id")
    if window_id and not IS_WINDOWS:
        for cmd in (["xdotool", "windowactivate", window_id],
                    ["wmctrl", "-i", "-a", hex(int(window_id))]):
            try:
                if subprocess.run(cmd, capture_output=True, timeout=2).returncode == 0:
                    return True
            except (OSError, ValueError, subprocess.SubprocessError):
                continue
    return False
