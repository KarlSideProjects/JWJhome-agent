"""Collect a bounded Docker CLI snapshot without exposing daemon errors."""

import json
import math
import re
import subprocess
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
    return int(float(match.group(1)) * base ** exponent)


def poll_once():
    observed_at = int(time.time())
    try:
        containers = _rows(["docker", "ps", "--all", "--no-trunc", "--format", "{{json .}}"])
        stats = _rows(["docker", "stats", "--all", "--no-stream", "--no-trunc", "--format", "{{json .}}"]) if containers else []
        if not all(isinstance(row, dict) and isinstance(row.get("ID"), str) and row["ID"] for row in containers + stats):
            raise ValueError("invalid Docker output")
        by_id = {row["ID"]: row for row in stats}
        result = []
        for row in containers[:MAX_CONTAINERS]:
            stat = by_id.get(row["ID"], {}) if row.get("State") == "running" else {}
            cpu = stat.get("CPUPerc", "")
            try:
                cpu = float(cpu.removesuffix("%"))
                if not math.isfinite(cpu) or cpu < 0:
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
        return {"status": "ok", "observed_at": observed_at, "containers": result, "truncated": len(containers) > MAX_CONTAINERS}
    except FileNotFoundError:
        return {"status": "unavailable", "observed_at": observed_at, "containers": [], "truncated": False}
    except (subprocess.SubprocessError, OSError, UnicodeError, ValueError, TypeError, OverflowError):
        return {"status": "error", "observed_at": observed_at, "containers": [], "truncated": False}
