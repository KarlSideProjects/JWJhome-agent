"""Collect a bounded, read-only snapshot from the local PVE node."""

import json
import os
import re
import selectors
import signal
import socket
import stat
import subprocess
import time


_PVESH = "/usr/bin/pvesh"
_MAX_OUTPUT = 1048576
_TIMEOUT = 15
_TOTAL_TIMEOUT = 30
_MAX_TOTAL = 900000
_MAX_DISKS = 64


def _run(args, timeout):
    for path in ("/", "/usr", "/usr/bin", _PVESH):
        info = os.lstat(path)
        if info.st_uid != 0 or info.st_mode & 0o022 or stat.S_ISLNK(info.st_mode):
            raise OSError("untrusted pvesh path")
        if path == _PVESH and (not stat.S_ISREG(info.st_mode) or not info.st_mode & 0o111):
            raise OSError("invalid pvesh executable")
        if path != _PVESH and not stat.S_ISDIR(info.st_mode):
            raise OSError("invalid pvesh directory")
    process = subprocess.Popen(args, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                               stderr=subprocess.DEVNULL, env={"PATH": "/usr/sbin:/usr/bin:/sbin:/bin", "LC_ALL": "C"},
                               start_new_session=True)
    deadline = time.monotonic() + timeout
    output = bytearray()
    completed = False
    try:
        with selectors.DefaultSelector() as selector:
            selector.register(process.stdout, selectors.EVENT_READ)
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0 or not selector.select(remaining):
                    raise TimeoutError
                chunk = os.read(process.stdout.fileno(), min(65536, _MAX_OUTPUT + 1 - len(output)))
                if not chunk:
                    break
                output.extend(chunk)
                if len(output) > _MAX_OUTPUT:
                    raise ValueError("output too large")
        if process.wait(timeout=max(0, deadline - time.monotonic())):
            raise RuntimeError("pvesh failed")
        completed = True
        return output
    finally:
        if not completed:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        process.wait()
        process.stdout.close()


def collect_snapshot(*, runner=_run, hostname=socket.gethostname, clock=time.time, monotonic=time.monotonic):
    node = hostname()
    if isinstance(node, str):
        node = node.split(".", 1)[0]
    if not isinstance(node, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9.-]{0,127}", node):
        raise ValueError("invalid local hostname")
    snapshot = {"version": 1, "node": node, "observed_at": int(clock()),
                "resources": None, "disks": None, "smart": {}, "errors": {}}

    deadline = monotonic() + _TOTAL_TIMEOUT
    total = 0

    def get(endpoint, *args):
        nonlocal total
        remaining = deadline - monotonic()
        if remaining <= 0:
            raise TimeoutError("snapshot deadline")
        raw = runner([_PVESH, "--noproxy", "get", endpoint, "--output-format", "json", *args], min(_TIMEOUT, remaining))
        if len(raw.encode() if isinstance(raw, str) else raw) > _MAX_OUTPUT:
            raise ValueError("output too large")
        value = json.loads(raw)
        total += len(json.dumps(value).encode())
        if total > _MAX_TOTAL:
            raise ValueError("snapshot too large")
        return value

    try:
        resources = get("/cluster/resources", "--type", "vm")
        if not isinstance(resources, list) or any(not isinstance(row, dict) for row in resources):
            raise ValueError
        snapshot["resources"] = [row for row in resources if row.get("node") == node]
    except (ValueError, TypeError, OSError, subprocess.SubprocessError, RuntimeError):
        snapshot["errors"]["resources"] = "collection_failed"

    try:
        disks = get(f"/nodes/{node}/disks/list")
        if not isinstance(disks, list) or any(not isinstance(row, dict) for row in disks):
            raise ValueError
        snapshot["disks"] = disks
    except (ValueError, TypeError, OSError, subprocess.SubprocessError, RuntimeError):
        snapshot["errors"]["disks"] = "collection_failed"
        return snapshot

    if len(disks) > _MAX_DISKS:
        snapshot["errors"]["disk_limit"] = len(disks) - _MAX_DISKS
    for row in disks[:_MAX_DISKS]:
        path = row.get("devpath") or row.get("path") or row.get("dev")
        if (not isinstance(path, str) or not re.fullmatch(r"/dev/[A-Za-z0-9_.-]+(?:/[A-Za-z0-9_.-]+)*", path)
                or any(part in {".", ".."} for part in path.split("/")) or len(path) > 512):
            snapshot["errors"]["invalid_disks"] = snapshot["errors"].get("invalid_disks", 0) + 1
            continue
        try:
            smart = get(f"/nodes/{node}/disks/smart", "--disk", path)
            if not isinstance(smart, dict):
                raise ValueError
            snapshot["smart"][path] = smart
        except (ValueError, TypeError, OSError, subprocess.SubprocessError, RuntimeError):
            snapshot["errors"].setdefault("smart", {})[path] = "collection_failed"
    return snapshot
