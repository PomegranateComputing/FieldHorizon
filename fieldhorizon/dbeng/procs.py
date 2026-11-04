"""
Who is running: this process's identity, and whether another local process is still alive.

Used to tell a live operation from a dead one after a restart. A process on
another host cannot be checked from here: callers get ``None`` ("unknown")
and must not treat it as dead.
"""
from __future__ import annotations

import os
import socket
import sys
from datetime import UTC, datetime

_STARTED_AT = datetime.now(UTC).isoformat()


def current_process() -> dict:
    return {"host": socket.gethostname(), "pid": os.getpid(), "started_at": _STARTED_AT}


def pid_alive(pid: int | None, host: str | None = None) -> bool | None:
    """True/False for a process on this host; None when it cannot be known (other host, no pid)."""
    if not pid or (host and host != socket.gethostname()):
        return None
    if pid == os.getpid():
        return True
    if sys.platform == "win32":
        return _windows_pid_alive(int(pid))
    try:
        os.kill(int(pid), 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


if sys.platform == "win32":
    # Defined on Windows only: ctypes.WinDLL and ctypes.get_last_error do not exist elsewhere, and type
    # checkers running on Linux must not see this body as reachable.
    def _windows_pid_alive(pid: int) -> bool:
        import ctypes
        from ctypes import wintypes

        process_query_limited_information = 0x1000
        still_active = 259
        error_access_denied = 5
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.OpenProcess.restype = wintypes.HANDLE
        kernel32.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
        kernel32.GetExitCodeProcess.argtypes = (wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD))
        kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
        handle = kernel32.OpenProcess(process_query_limited_information, False, pid)
        if not handle:
            # Access denied means the process exists (another user's); anything else means it does not.
            return ctypes.get_last_error() == error_access_denied
        try:
            code = wintypes.DWORD()
            if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
                return True
            return code.value == still_active
        finally:
            kernel32.CloseHandle(handle)
