"""Task commands for the suite, written once for both shells.

Shell tasks run under /bin/sh on POSIX and cmd.exe on Windows. That is the
product's behaviour and what users write, so the suite does not paper over it.
The suite's own task commands, though, are POSIX one-liners (printf, `>>`,
`test -f`) that cmd.exe cannot run, and almost every test uses them only as a
stand-in for "a task that succeeds", "fails", or "leaves a trace". On POSIX each
helper returns exactly the command the tests always used. On Windows it returns
the same effect as a Python one-liner, which both shells run identically.

A test that is about a shell's own semantics writes its command directly.
"""

import os
import shlex
import sys

WINDOWS = os.name == "nt"

_PYTHON = f'"{sys.executable}"'


def _python(code: str) -> str:
    # Single quotes only inside: the code sits in double quotes for both shells.
    # Paths and text go in raw literals, because a rendered Windows path such as
    # C:\Users\... is an invalid escape in an ordinary one.
    assert '"' not in code, code
    return f'{_PYTHON} -c "{code}"'


def emit(text: str) -> str:
    """Print `text` to stdout, without a trailing newline."""
    if WINDOWS:
        return _python(f"import sys; sys.stdout.write(r'{text}')")
    return f"printf {shlex.quote(text)}"


def append(text: str, path, newline: bool = False) -> str:
    """Append `text` to the file at `path`: a trace that the task ran."""
    if WINDOWS:
        return _python(f"open(r'{path}', 'a').write(r'{text}' + chr(10) * {int(newline)})")
    if newline:
        return f'printf "%s\\n" {shlex.quote(text)} >> "{path}"'
    return f'printf {shlex.quote(text)} >> "{path}"'


def write(text: str, path) -> str:
    """Replace the file at `path` with `text`."""
    if WINDOWS:
        return _python(f"open(r'{path}', 'w').write(r'{text}')")
    return f'printf "%s" {shlex.quote(text)} > "{path}"'


def touch(path) -> str:
    if WINDOWS:
        return _python(f"open(r'{path}', 'a').close()")
    return f'touch "{path}"'


def exists(path) -> str:
    """Succeed only if the file at `path` exists."""
    if WINDOWS:
        return _python(f"import os, sys; sys.exit(0 if os.path.isfile(r'{path}') else 1)")
    return f'test -f "{path}"'


def pause(seconds: float) -> str:
    if WINDOWS:
        return _python(f"import time; time.sleep({seconds})")
    return f"sleep {seconds}"


def wait_for(path, seconds: float = 10) -> str:
    """Succeed as soon as the file at `path` appears; fail after `seconds`."""
    ticks = int(seconds * 10)
    if WINDOWS:
        # time.sleep returns None, so any() keeps polling until the file exists.
        return _python(f"import os, sys, time; sys.exit(0 if any(os.path.isfile(r'{path}') "
                       f"or time.sleep(0.1) for _ in range({ticks})) else 1)")
    return (f'for i in $(seq 1 {ticks}); do [ -f "{path}" ] && exit 0; sleep 0.1; done; '
            "exit 1")


def record_pid_and_sleep(pidfile, seconds: int) -> str:
    """Write the PID of the process doing the work, then keep it busy.

    The recorded PID is the long-running process itself — on POSIX `exec`
    keeps the shell's PID, on Windows it is Python's, a child of cmd.exe — so a
    test can check that exact process died, not merely its parent.
    """
    if WINDOWS:
        return _python(f"import os, time; open(r'{pidfile}', 'w').write(str(os.getpid())); "
                       f"time.sleep({seconds})")
    return f'echo $$ > "{pidfile}"; exec sleep {seconds}'


def then(*commands: str) -> str:
    """Run commands in order; the last one's exit status is the task's."""
    return (" & " if WINDOWS else "; ").join(commands)


def process_alive(pid: int) -> bool:
    if not WINDOWS:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        return True
    # os.kill(pid, 0) is not a probe on Windows: 0 is CTRL_C_EVENT, which it
    # would send to the process group.
    import ctypes
    kernel32 = ctypes.windll.kernel32
    handle = kernel32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
    if not handle:
        return False
    try:
        code = ctypes.c_ulong()
        kernel32.GetExitCodeProcess(handle, ctypes.byref(code))
        return code.value == 259  # STILL_ACTIVE
    finally:
        kernel32.CloseHandle(handle)
