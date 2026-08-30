"""Notebook launcher handed to the TASK environment's interpreter by path.

Replaces `python -m papermill` so the Jupyter kernel talks over IPC (Unix
domain sockets, protected by file permissions) instead of loopback TCP.
ipykernel >= 6.30 warns that TCP kernel channels are unencrypted; on a
shared machine they are also connectable by any local process that guesses
the port, while IPC sockets are not. ZeroMQ has no ipc:// transport on
Windows, so TCP-on-loopback remains the fallback there.

Self-contained on purpose: the task environment only guarantees papermill
and ipykernel (see environments._RUNTIME_PACKAGES) — never runrail itself.
"""

import os
import sys

# Running this file by path puts its own directory (runrail/worker/) first
# on sys.path, where queue.py/service.py/runners.py would shadow stdlib and
# third-party modules — jupyter_client imports `queue` and gets ours. Scrub
# it before anything else is imported.
_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path[:] = [p for p in sys.path
               if os.path.abspath(p) != _HERE and p != _HERE]

import argparse  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="Execute a notebook via papermill")
    parser.add_argument("input_path")
    parser.add_argument("output_path")
    parser.add_argument("--kernel", default="python3")
    parser.add_argument("-p", "--parameters", nargs=2, action="append",
                        default=[], metavar=("KEY", "VALUE"))
    args = parser.parse_args()

    if os.name != "nt":
        # papermill's CLI/API exposes no kernel-transport knob, so flip the
        # trait default before any KernelManager is instantiated.
        from jupyter_client import KernelManager
        KernelManager.transport.default_value = "ipc"

    import papermill

    try:
        # The same value inference the papermill CLI applies to -p pairs.
        from papermill.cli import _resolve_type
    except ImportError:  # private API — fall back to raw strings
        def _resolve_type(value):
            return value

    papermill.execute_notebook(
        args.input_path, args.output_path,
        parameters={key: _resolve_type(value) for key, value in args.parameters},
        kernel_name=args.kernel,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
