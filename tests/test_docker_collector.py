import json
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from types import SimpleNamespace

import pytest

from hwmonitor_mqtt.collectors import docker


def test_snapshot_combines_full_ids_and_handles_windows_memory(monkeypatch):
    calls = []
    def run(args, **kwargs):
        calls.append(args)
        rows = ([{"ID": "abc", "Names": "web", "Image": "img", "State": "running"},
                 {"ID": "def", "Names": "old", "Image": "img", "State": "exited"}]
                if args[1] == "ps" else
                [{"ID": "abc", "CPUPerc": "1.25%", "MemUsage": "38.21 MiB"},
                 {"ID": "def", "CPUPerc": "0.00%", "MemUsage": "0B / 1GB"}])
        return SimpleNamespace(stdout="\n".join(map(json.dumps, rows)).encode())
    monkeypatch.setattr(docker.subprocess, "run", run)
    result = docker.poll_once()
    assert result["status"] == "ok"
    assert result["containers"][0]["memory_usage_bytes"] == int(38.21 * 1024 ** 2)
    assert result["containers"][0]["memory_limit_bytes"] is None
    assert result["containers"][0]["cpu_percent"] == 1.25
    assert result["containers"][1]["cpu_percent"] is None
    assert all("--no-trunc" in call for call in calls)


def test_empty_and_bounded(monkeypatch):
    monkeypatch.setattr(docker, "_rows", lambda args: [] if args[1] == "stats" else [{"ID": str(i), "State": "running", "Image": "x" * 1000} for i in range(31)])
    result = docker.poll_once()
    assert result["status"] == "ok" and result["truncated"]
    assert len(result["containers"]) == 30
    assert len(result["containers"][0]["image"]) == 128
    assert result["containers"][0]["cpu_percent"] is None
    monkeypatch.setattr(docker, "_rows", lambda args: [])
    assert docker.poll_once()["status"] == "ok"
    assert docker.poll_once()["containers"] == []


@pytest.mark.parametrize(("failure", "status"), [(FileNotFoundError(), "unavailable"), (subprocess.TimeoutExpired("docker", 10), "error"), (subprocess.CalledProcessError(1, "docker"), "error"), (ValueError("secret"), "error")])
def test_failures_clear_data_and_do_not_expose_errors(monkeypatch, failure, status):
    def fail(args):
        raise failure
    monkeypatch.setattr(docker, "_rows", fail)
    result = docker.poll_once()
    assert result["status"] == status
    assert result["containers"] == []
    assert "secret" not in str(result)


@pytest.mark.parametrize(("value", "expected"), [("12B", 12), ("1.5kB", 1500), ("2MB", 2_000_000), ("1GiB", 1024 ** 3), ("1.25TiB", int(1.25 * 1024 ** 4)), ("--", None)])
def test_memory_units(value, expected):
    assert docker._bytes(value) == expected


def test_linux_memory_and_command_bounds(monkeypatch):
    def run(args, **kwargs):
        assert kwargs == {"capture_output": True, "timeout": 10, "check": True}
        row = {"ID": "abc", "State": "running"} if args[1] == "ps" else {"ID": "abc", "MemUsage": "1.5MB / 2GiB"}
        return SimpleNamespace(stdout=json.dumps(row).encode())
    monkeypatch.setattr(docker.subprocess, "run", run)
    row = docker.poll_once()["containers"][0]
    assert row["memory_usage_bytes"] == 1_500_000
    assert row["memory_limit_bytes"] == 2 * 1024 ** 3


@pytest.mark.parametrize("output", [b"{bad", b'[]', b'{"ID": null}'])
def test_invalid_cli_json_is_error(monkeypatch, output):
    monkeypatch.setattr(docker.subprocess, "run", lambda *args, **kwargs: SimpleNamespace(stdout=output))
    assert docker.poll_once()["status"] == "error"
