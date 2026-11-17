"""Collect a bounded Docker CLI snapshot without exposing daemon errors."""

import json
import math
import os
from pathlib import Path
import re
import subprocess
import shutil
import socket
import time

MAX_CONTAINERS = 30
MAX_TEXT = 128
TIMEOUT_SEC = 10


def _rows(args):
    result = subprocess.run(args, capture_output=True, timeout=TIMEOUT_SEC, check=True)
    return [json.loads(line) for line in result.stdout.decode("utf-8").splitlines() if line.strip()]


def _bytes(value):
    match = re.fullmatch(r"\s*(\d+(?:\.\d+)?)\s*([KMGTPE]?i?B)\s*", value, re.I)
    if not match:
        return None
    unit = match.group(2).upper()
    exponent = "KMGTPE".index(unit[0]) + 1 if unit != "B" else 0
    base = 1024 if "I" in unit else 1000
    result = int(float(match.group(1)) * base ** exponent)
    return result if result <= 2**63 - 1 else None


def _docker_executable():
    configured = os.environ.get("DOCKER_CLI_PATH")
    if configured:
        return configured
    found = shutil.which("docker")
    if found:
        return found
    for variable, parts in (("ProgramFiles", ("Docker", "Docker", "resources", "bin", "docker.exe")),
                            ("LOCALAPPDATA", ("Programs", "DockerDesktop", "resources", "bin", "docker.exe"))):
        base = os.environ.get(variable)
        if base:
            candidate = Path(base).joinpath(*parts)
            if candidate.is_file():
                return str(candidate)
    return "docker"


def initial_snapshot(node):
    return {"version": 1, "provider": "docker", "node": node, "status": "error",
            "observed_at": None, "containers": [], "truncated": False}


def poll_once(node=None):
    node = node or socket.gethostname()
    envelope = initial_snapshot(node)
    executable = _docker_executable()
    observed_at = int(time.time())
    try:
        containers = _rows([executable, "ps", "--all", "--no-trunc", "--format", "{{json .}}"])
        stats = _rows([executable, "stats", "--all", "--no-stream", "--no-trunc", "--format", "{{json .}}"]) if containers else []
        if not all(isinstance(row, dict) and isinstance(row.get("ID"), str) and re.fullmatch(r"[a-f0-9]{64}", row["ID"]) for row in containers + stats):
            raise ValueError("invalid Docker output")
        if len({row["ID"] for row in containers}) != len(containers):
            raise ValueError("duplicate Docker ID")
        by_id = {row["ID"]: row for row in stats}
        result = []
        for row in containers[:MAX_CONTAINERS]:
            stat = by_id.get(row["ID"], {}) if row.get("State") == "running" else {}
            cpu = stat.get("CPUPerc", "")
            try:
                cpu = float(cpu.removesuffix("%"))
                if not math.isfinite(cpu) or not 0 <= cpu <= 409600:
                    cpu = None
            except (ValueError, TypeError, AttributeError):
                cpu = None
            memory = stat.get("MemUsage", "")
            parts = memory.split("/", 1) if isinstance(memory, str) else []
            result.append({
                "id": row["ID"][:MAX_TEXT],
                "name": str(row.get("Names", ""))[:MAX_TEXT],
                "image": str(row.get("Image", ""))[:MAX_TEXT],
                "state": str(row.get("State", ""))[:MAX_TEXT],
                "cpu_percent": cpu,
                "memory_usage_bytes": _bytes(parts[0]) if parts else None,
                "memory_limit_bytes": _bytes(parts[1]) if len(parts) == 2 else None,
            })
        guests = []
        for row in result:
            used, total = row["memory_usage_bytes"], row["memory_limit_bytes"]
            guests.append({
                "id": "docker:" + row["id"], "container_id": row["id"], "type": "docker", "node": node,
                "name": row["name"] or row["id"][:12], "status": row["state"] or "unknown",
                "uptime": None, "vcpus": None, "memory_basis": "docker_cli",
                "cpu": {"percent_total": row["cpu_percent"]},
                "memory": {"ram": {"used": used, "total": total,
                                   "percent": round(used / total * 100, 1) if used is not None and total else None}},
                "network_io": {"total": {"rate": {"rx_bytes_per_s": None, "tx_bytes_per_s": None}}},
                "disk_io": {"total": {"rate": {"read_bytes_per_s": None, "write_bytes_per_s": None}}},
            })
        return dict(envelope, status="ok", observed_at=observed_at, containers=result,
                    truncated=len(containers) > MAX_CONTAINERS, guests=guests)
    except (subprocess.SubprocessError, OSError, UnicodeError, ValueError, TypeError, OverflowError):
        return dict(envelope, observed_at=observed_at)
