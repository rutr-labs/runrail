"""Notebook launcher handed to the TASK environment's interpreter by path.

Replaces `python -m papermill` so the Jupyter kernel's channels are not open
plain text on loopback TCP. ipykernel warns when they are ("Kernel is running
over TCP without encryption"), and on a shared machine any local process that
guesses the port can connect to them.

- macOS and Linux: IPC (Unix domain sockets, protected by file permissions).
- Windows, where ZeroMQ has no ipc:// transport: TCP, encrypted with CurveZMQ
  keys the kernel manager provisions for each kernel (jupyter_client >= 8.10,
  a kernel that declares support, and a libzmq built with CURVE). Without all
  three the channels stay plain TCP and the kernel's warning stays, which is
  then the truth.

RUNRAIL_KERNEL_TRANSPORT=tcp forces the TCP path anywhere, for a filesystem
where Unix sockets misbehave, and so that path can be tested off Windows.

Self-contained on purpose: the task environment only guarantees papermill,
ipykernel and jupyter_client (see environments._RUNTIME_PACKAGES) — never
runrail itself.
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


def _secure_kernel_channels() -> None:
    """Choose the kernel's transport, and encrypt it when it has to be TCP.

    papermill's CLI and API expose neither knob, so the trait defaults are set
    before any KernelManager exists; nbclient's AsyncKernelManager inherits
    them."""
    from jupyter_client import KernelManager

    transport = os.environ.get("RUNRAIL_KERNEL_TRANSPORT") or ("tcp" if os.name == "nt" else "ipc")
    KernelManager.transport.default_value = transport
    if transport != "tcp":
        return
    # "auto" provisions CurveZMQ keys only for a kernel whose kernelspec says
    # it supports them (ipykernel 7 does), so another kernel still starts.
    encryption = KernelManager.class_traits().get("transport_encryption")
    if encryption is None:
        return  # jupyter_client < 8.10: no manager-side encryption to ask for
    import zmq
    if zmq.has("curve"):
        encryption.default_value = "auto"


def main() -> int:
    parser = argparse.ArgumentParser(description="Execute a notebook via papermill")
    parser.add_argument("input_path")
    parser.add_argument("output_path")
    parser.add_argument("--kernel", default="python3")
    parser.add_argument("-p", "--parameters", nargs=2, action="append",
                        default=[], metavar=("KEY", "VALUE"))
    args = parser.parse_args()

    _secure_kernel_channels()

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
