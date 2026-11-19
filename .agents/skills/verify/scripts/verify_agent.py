#!/usr/bin/env python3
"""Disposable verification harness for JWJhome-agent CLI and collector surfaces.

Manages isolated run directories, read-only doctor checks, literal command
driving with structured evidence capture outside the run directory, and safe
cleanup that preserves proof artifacts.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tarfile
from typing import Any


REPO_ROOT = Path(__file__).resolve().parents[4]

FAKE_DOCKER_PY = r'''import json
import os
import sys

mode = os.environ.get("VERIFY_DOCKER_MODE", "ok")
args = sys.argv[1:]
subcmd = args[0] if args else ""

if mode == "error":
    sys.stderr.write("Cannot connect to the Docker daemon\n")
    sys.exit(1)

if mode == "truncated":
    count = 32
    if subcmd == "ps":
        for i in range(count):
            cid = f"{i + 1:064x}"
            print(json.dumps({"ID": cid, "Names": f"svc-{i:02d}", "Image": "alpine:3.20", "State": "running"}))
        sys.exit(0)
    if subcmd == "stats":
        for i in range(count):
            cid = f"{i + 1:064x}"
            print(json.dumps({"ID": cid, "CPUPerc": "1.25%", "MemUsage": "16MiB / 256MiB"}))
        sys.exit(0)

if subcmd == "ps":
    rows = [
        {"ID": "a" * 64, "Names": "mqtt-broker", "Image": "eclipse-mosquitto:2", "State": "running"},
        {"ID": "b" * 64, "Names": "backup-job", "Image": "busybox:1.36", "State": "exited"},
    ]
    for row in rows:
        print(json.dumps(row))
    sys.exit(0)

if subcmd == "stats":
    rows = [
        {"ID": "a" * 64, "CPUPerc": "4.50%", "MemUsage": "128MiB / 1GiB"},
        {"ID": "b" * 64, "CPUPerc": "99.9%", "MemUsage": "512MiB / 1GiB"},
    ]
    for row in rows:
        print(json.dumps(row))
    sys.exit(0)

sys.stderr.write(f"unexpected docker args: {args}\n")
sys.exit(2)
'''

FAKE_SMARTCTL_PY = r'''import json
import os
import sys

mode = os.environ.get("VERIFY_SMART_MODE", "healthy")
args = sys.argv[1:]

if mode == "error":
    sys.stderr.write("smartctl: permission denied\n")
    sys.exit(1)

if "--scan" in args:
    print(json.dumps({"devices": [{"name": "/dev/nvme0", "type": "nvme"}]}))
    sys.exit(0)

if "-a" in args:
    if mode == "critical":
        payload = {
            "device": {"protocol": "NVMe"},
            "model_name": "Verify NVMe 1TB",
            "serial_number": "VERIFY-NVME-001",
            "smart_status": {"passed": False},
            "nvme_smart_health_information_log": {
                "critical_warning": 1,
                "temperature": 355,
                "available_spare": 5,
                "available_spare_threshold": 10,
                "percentage_used": 100,
                "media_errors": 3,
                "power_on_hours": 12000,
            },
        }
    else:
        payload = {
            "device": {"protocol": "NVMe"},
            "model_name": "Verify NVMe 1TB",
            "serial_number": "VERIFY-NVME-001",
            "smart_status": {"passed": True},
            "nvme_smart_health_information_log": {
                "critical_warning": 0,
                "temperature": 310,
                "available_spare": 100,
                "available_spare_threshold": 10,
                "percentage_used": 4,
                "media_errors": 0,
                "power_on_hours": 1500,
            },
        }
    print(json.dumps(payload))
    sys.exit(0)

sys.stderr.write(f"unexpected smartctl args: {args}\n")
sys.exit(2)
'''


def _git_head(repo_root: Path) -> str:
    proc = subprocess.run(
        ["git", "-C", str(repo_root), "rev-parse", "HEAD"],
        capture_output=True,
        text=True,
        check=False,
    )
    return proc.stdout.strip() if proc.returncode == 0 else "unknown"


def _is_within(child: Path, parent: Path) -> bool:
    try:
        child.resolve().relative_to(parent.resolve())
        return True
    except ValueError:
        return False


def _write_tar_gz(path: Path, entries: list[tuple[str, bytes | None, int]]) -> None:
    with tarfile.open(path, "w:gz") as archive:
        for name, content, mode in entries:
            info = tarfile.TarInfo(name=name)
            info.mode = mode
            if content is None:
                info.type = tarfile.DIRTYPE
                info.size = 0
                archive.addfile(info)
            else:
                info.type = tarfile.REGTYPE
                info.size = len(content)
                archive.addfile(info, io.BytesIO(content))


def _write_shim(shims_dir: Path, name: str, script_source: str, python_exe: str) -> Path:
    py_path = shims_dir / f"{name}.py"
    py_path.write_text(script_source, encoding="utf-8")
    if os.name == "nt":
        wrapper = shims_dir / f"{name}.cmd"
        wrapper.write_text(f'@echo off\r\n"{python_exe}" "{py_path}" %*\r\n', encoding="utf-8")
    else:
        wrapper = shims_dir / name
        wrapper.write_text(f'#!/bin/sh\nexec "{python_exe}" "{py_path}" "$@"\n', encoding="utf-8")
        wrapper.chmod(0o755)
    return wrapper


def cmd_launch(args: argparse.Namespace) -> int:
    run_dir = Path(args.run_dir).resolve()
    evidence_dir = Path(args.evidence_dir).resolve()
    if _is_within(evidence_dir, run_dir) or _is_within(run_dir, evidence_dir):
        print(json.dumps({"ok": False, "error": "run-dir and evidence-dir must not overlap"}, indent=2))
        return 2
    if run_dir.exists():
        print(json.dumps({"ok": False, "error": f"run-dir already exists: {run_dir}"}, indent=2))
        return 2

    shims_dir = run_dir / "shims"
    archives_dir = run_dir / "archives"
    staging_dir = run_dir / "staging"
    for directory in (shims_dir, archives_dir, staging_dir, evidence_dir):
        directory.mkdir(parents=True, exist_ok=True)

    python_exe = sys.executable
    docker_shim = _write_shim(shims_dir, "fake-docker", FAKE_DOCKER_PY, python_exe)
    smartctl_shim = _write_shim(shims_dir, "fake-smartctl", FAKE_SMARTCTL_PY, python_exe)

    valid_archive = archives_dir / "valid-source.tar.gz"
    _write_tar_gz(
        valid_archive,
        [
            ("JWJhome-agent-verify", None, 0o755),
            ("JWJhome-agent-verify/README.md", b"# Verified source fixture\n", 0o644),
            ("JWJhome-agent-verify/run.sh", b"#!/bin/sh\necho ok\n", 0o755),
        ],
    )

    traversal_archive = archives_dir / "traversal-source.tar.gz"
    _write_tar_gz(
        traversal_archive,
        [
            ("JWJhome-agent-verify", None, 0o755),
            ("JWJhome-agent-verify/../escape.txt", b"escaped\n", 0o644),
        ],
    )

    state = {
        "repo_root": str(REPO_ROOT),
        "git_head": _git_head(REPO_ROOT),
        "python_exe": python_exe,
        "run_dir": str(run_dir),
        "evidence_dir": str(evidence_dir),
        "shims": {
            "docker": str(docker_shim),
            "smartctl": str(smartctl_shim),
        },
        "archives": {
            "valid": str(valid_archive),
            "traversal": str(traversal_archive),
        },
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    (run_dir / "state.json").write_text(json.dumps(state, indent=2), encoding="utf-8")
    print(json.dumps({"ok": True, "state": state}, indent=2))
    return 0


def _load_state(run_dir: Path) -> dict[str, Any]:
    state_file = run_dir / "state.json"
    if not state_file.is_file():
        raise FileNotFoundError(f"missing state.json in {run_dir}")
    return json.loads(state_file.read_text(encoding="utf-8"))


def cmd_doctor(args: argparse.Namespace) -> int:
    run_dir = Path(args.run_dir).resolve()
    checks: dict[str, Any] = {}
    ok = True
    try:
        state = _load_state(run_dir)
    except Exception as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, indent=2))
        return 1

    evidence_dir = Path(state["evidence_dir"]).resolve()
    checks["disjoint_evidence_dir"] = not (_is_within(evidence_dir, run_dir) or _is_within(run_dir, evidence_dir))
    checks["evidence_dir_exists"] = evidence_dir.is_dir()
    checks["repo_root_matches"] = Path(state["repo_root"]).resolve() == REPO_ROOT
    current_head = _git_head(REPO_ROOT)
    checks["git_head_matches"] = current_head == state["git_head"]
    checks["git_head"] = current_head

    for key, path_str in state.get("shims", {}).items():
        checks[f"shim_{key}_exists"] = Path(path_str).is_file()
    for key, path_str in state.get("archives", {}).items():
        checks[f"archive_{key}_exists"] = Path(path_str).is_file()

    probe = subprocess.run(
        [
            state["python_exe"],
            "-c",
            "import hwmonitor_mqtt.platforms.install, hwmonitor_mqtt.collectors.docker, hwmonitor_mqtt.collectors.smart; print('ok')",
        ],
        cwd=str(REPO_ROOT),
        capture_output=True,
        text=True,
        check=False,
    )
    checks["imports_ok"] = probe.returncode == 0 and probe.stdout.strip() == "ok"
    if not checks["imports_ok"]:
        checks["imports_stderr"] = probe.stderr.strip()

    ok = all(value is True for key, value in checks.items() if key not in {"git_head", "imports_stderr"})
    print(json.dumps({"ok": ok, "run_dir": str(run_dir), "evidence_dir": str(evidence_dir), "checks": checks}, indent=2))
    return 0 if ok else 1


def cmd_drive(args: argparse.Namespace) -> int:
    run_dir = Path(args.run_dir).resolve()
    state = _load_state(run_dir)
    evidence_dir = Path(state["evidence_dir"]).resolve()
    command = list(args.command)
    if command and command[0] == "--":
        command = command[1:]
    if not command:
        print(json.dumps({"ok": False, "error": "no command provided after --"}, indent=2))
        return 2

    env = os.environ.copy()
    env_overrides: dict[str, str] = {}
    for item in args.env or []:
        key, sep, value = item.partition("=")
        if not sep:
            print(json.dumps({"ok": False, "error": f"invalid --env value: {item}"}, indent=2))
            return 2
        env[key] = value
        env_overrides[key] = value

    cwd = Path(args.cwd).resolve() if args.cwd else REPO_ROOT
    proc = subprocess.run(command, cwd=str(cwd), env=env, capture_output=True, text=True, check=False)

    feature_dir = evidence_dir / args.feature
    feature_dir.mkdir(parents=True, exist_ok=True)
    proof_path = feature_dir / f"{args.entry}.json"
    record = {
        "feature": args.feature,
        "entry_point": args.entry,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "git_head": _git_head(REPO_ROOT),
        "cwd": str(cwd),
        "env_overrides": env_overrides,
        "command": command,
        "exit_code": proc.returncode,
        "stdout": proc.stdout,
        "stderr": proc.stderr,
    }
    proof_path.write_text(json.dumps(record, indent=2), encoding="utf-8")
    print(json.dumps({
        "ok": True,
        "proof_path": str(proof_path),
        "exit_code": proc.returncode,
        "stdout": proc.stdout,
        "stderr": proc.stderr,
    }, indent=2))
    return 0


def cmd_cleanup(args: argparse.Namespace) -> int:
    run_dir = Path(args.run_dir).resolve()
    state = _load_state(run_dir)
    evidence_dir = Path(state["evidence_dir"]).resolve()
    if _is_within(evidence_dir, run_dir):
        print(json.dumps({"ok": False, "error": "refusing to delete run-dir containing evidence-dir"}, indent=2))
        return 2
    shutil.rmtree(run_dir)
    surviving = sorted(str(p.relative_to(evidence_dir)) for p in evidence_dir.rglob("*") if p.is_file()) if evidence_dir.exists() else []
    print(json.dumps({
        "ok": True,
        "removed_run_dir": str(run_dir),
        "run_dir_exists": run_dir.exists(),
        "evidence_dir": str(evidence_dir),
        "evidence_dir_exists": evidence_dir.exists(),
        "surviving_artifacts": surviving,
    }, indent=2))
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="action", required=True)

    p_launch = sub.add_parser("launch", help="Create isolated disposable run directory and seed fixtures/shims")
    p_launch.add_argument("--run-dir", required=True)
    p_launch.add_argument("--evidence-dir", required=True)
    p_launch.set_defaults(func=cmd_launch)

    p_doctor = sub.add_parser("doctor", help="Read-only health check on an initialized run directory")
    p_doctor.add_argument("--run-dir", required=True)
    p_doctor.set_defaults(func=cmd_doctor)

    p_drive = sub.add_parser("drive", help="Run a literal command and record structured evidence")
    p_drive.add_argument("--run-dir", required=True)
    p_drive.add_argument("--feature", required=True)
    p_drive.add_argument("--entry", required=True)
    p_drive.add_argument("--cwd")
    p_drive.add_argument("--env", action="append", default=[])
    p_drive.add_argument("command", nargs=argparse.REMAINDER)
    p_drive.set_defaults(func=cmd_drive)

    p_cleanup = sub.add_parser("cleanup", help="Remove disposable run directory while preserving evidence")
    p_cleanup.add_argument("--run-dir", required=True)
    p_cleanup.set_defaults(func=cmd_cleanup)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
