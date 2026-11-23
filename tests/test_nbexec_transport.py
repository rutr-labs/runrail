"""The notebook launcher keeps the kernel's channels off plain-text TCP.

ipykernel prints "Kernel is running over TCP without encryption" whenever they
are on it. On macOS and Linux nbexec uses IPC sockets; on Windows, which has
no IPC in ZeroMQ, it encrypts TCP with CurveZMQ. RUNRAIL_KERNEL_TRANSPORT=tcp
takes the Windows path on any platform, which is how it is tested here.

Runs a real kernel, so it needs papermill and ipykernel in this interpreter;
it is skipped without them (they belong to task environments, not RunRail).
"""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

pytest.importorskip("papermill")
pytest.importorskip("ipykernel")
jupyter_client = pytest.importorskip("jupyter_client")

import runrail.worker.runners as runners  # noqa: E402

NBEXEC = Path(runners.__file__).with_name("nbexec.py")
WARNING = "Kernel is running over TCP without encryption"


def _run(tmp_path: Path, transport: str | None):
    notebook = tmp_path / "hello.ipynb"
    notebook.write_text(json.dumps({
        "nbformat": 4, "nbformat_minor": 5,
        "metadata": {"kernelspec": {"name": "python3", "display_name": "Python 3", "language": "python"}},
        "cells": [{"cell_type": "code", "id": "a", "metadata": {}, "execution_count": None,
                   "outputs": [], "source": "print('hello from the kernel')"}],
    }))
    output = tmp_path / "out.ipynb"
    env = {key: value for key, value in os.environ.items() if key != "RUNRAIL_KERNEL_TRANSPORT"}
    if transport:
        env["RUNRAIL_KERNEL_TRANSPORT"] = transport
    done = subprocess.run([sys.executable, str(NBEXEC), str(notebook), str(output), "--kernel", "python3"],
                          capture_output=True, text=True, env=env, timeout=180)
    assert done.returncode == 0, done.stderr
    printed = "".join("".join(o.get("text", "")) for cell in json.loads(output.read_text())["cells"]
                      for o in cell.get("outputs", []))
    assert "hello from the kernel" in printed
    return done.stderr


@pytest.mark.skipif(os.name == "nt", reason="ZeroMQ has no IPC transport on Windows")
def test_the_default_kernel_transport_is_not_plain_tcp(tmp_path):
    assert WARNING not in _run(tmp_path, None)


def test_a_tcp_kernel_is_encrypted(tmp_path):
    import zmq
    from jupyter_client import KernelManager
    if "transport_encryption" not in KernelManager.class_traits():
        pytest.skip("jupyter_client < 8.10 cannot encrypt kernel channels")
    if not zmq.has("curve"):
        pytest.skip("this libzmq was built without CURVE")
    assert WARNING not in _run(tmp_path, "tcp")


def test_task_environments_get_a_jupyter_client_that_can_encrypt():
    from runrail.environments import _RUNTIME_PACKAGES
    assert any(spec.startswith("jupyter_client>=8.10") for spec in _RUNTIME_PACKAGES)
