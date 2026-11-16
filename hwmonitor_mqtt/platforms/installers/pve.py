"""Native systemd installation for a detected PVE host."""

import argparse
import grp
import os
from pathlib import Path
import shutil
import stat
import subprocess
import sys
import tempfile
import time

from hwmonitor_mqtt.clients.pve import PveApiClient, PveConfig, PveError


SERVICE_NAME = "hwmonitor-mqtt-pve.service"
SNAPSHOT_SERVICE = "hwmonitor-mqtt-pve-snapshot.service"
SNAPSHOT_TIMER = "hwmonitor-mqtt-pve-snapshot.timer"
READER_GROUP = "hwmonitor-mqtt-pve-readers"
SNAPSHOT_PATH = "/run/hwmonitor-mqtt-pve/snapshot.json"
DEFAULT_CONFIG = "/etc/hwmonitor-mqtt/pve.env"
INSTALL_ROOT = "/opt/hwmonitor-mqtt-pve"
APP_ROOT = f"{INSTALL_ROOT}/app"
SERVICE_VENV = f"{INSTALL_ROOT}/venv"
SYSTEMD_DIR = "/etc/systemd/system"


def _validate_trusted_path(path):
    path = Path(path)
    if not path.is_absolute() or ".." in path.parts:
        raise RuntimeError("untrusted PVE installation path")
    fd = os.open("/", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        for part in path.parts[1:]:
            info = os.fstat(fd)
            if info.st_uid != 0 or info.st_mode & 0o022 or not stat.S_ISDIR(info.st_mode):
                raise RuntimeError("untrusted PVE installation path")
            try:
                next_fd = os.open(part, os.O_PATH | os.O_NOFOLLOW, dir_fd=fd)
            except FileNotFoundError:
                return
            os.close(fd)
            fd = next_fd
            info = os.fstat(fd)
            if info.st_uid != 0 or info.st_mode & 0o022 or stat.S_ISLNK(info.st_mode):
                raise RuntimeError("untrusted PVE installation path")
    finally:
        os.close(fd)


def _validate_trusted_tree(root):
    root = Path(root)
    _validate_trusted_path(root)
    if not root.exists():
        return
    for directory, subdirs, files in os.walk(root, followlinks=False):
        for name in subdirs + files:
            path = Path(directory) / name
            if path.is_symlink():
                info = path.lstat()
                if info.st_uid != 0:
                    raise RuntimeError("untrusted PVE installation link")
                target = path.resolve(strict=True)
                _validate_trusted_path(target)
            else:
                _validate_trusted_path(path)


def _validate_executable(path):
    path = Path(path)
    _validate_trusted_path(path.parent)
    target = path.resolve(strict=True)
    _validate_trusted_path(target)
    if not target.is_file() or not os.access(target, os.X_OK):
        raise RuntimeError("untrusted PVE executable")


def command(project_root, config_file=DEFAULT_CONFIG):
    entry = [str(Path(shutil.which(sys.argv[0]) or sys.argv[0]).resolve()), "--native-install"] if "__compiled__" in globals() else [sys.executable, "-m", "hwmonitor_mqtt.platforms.installers.pve"]
    return entry + ["--project-root", str(Path(project_root).resolve()), "--config-file", str(Path(config_file).resolve())]


def readiness(config_file=DEFAULT_CONFIG):
    try:
        config = PveConfig.from_environment(env={}, env_file=config_file)
        if config is None:
            return "PVE configuration is unconfigured"
        PveApiClient(config).validate_scope()
        return None
    except PveError as error:
        return f"PVE validation failed: {error}"


def _unit_escape(value):
    return str(value).replace("\\", "\\\\").replace(" ", "\\x20").replace("\t", "\\x09").replace('"', "\\x22").replace("%", "%%")


def unit_text(project_root, python=f"{SERVICE_VENV}/bin/python", config_file=DEFAULT_CONFIG, compiled=False):
    root = Path(project_root).resolve()
    config_file = Path(config_file).resolve()
    return ("[Unit]\nDescription=Hardware Monitor MQTT sender for Proxmox VE\nAfter=network-online.target\nWants=network-online.target\n\n"
            "[Service]\nType=simple\nDynamicUser=yes\n" + ("" if compiled else "SupplementaryGroups=" + READER_GROUP + "\nEnvironment=PVE_LOCAL_SNAPSHOT=" + SNAPSHOT_PATH + "\n") + "WorkingDirectory=" + _unit_escape(APP_ROOT) + "\n"
            "EnvironmentFile=-" + _unit_escape(root / ".env") + "\nEnvironmentFile=-" + _unit_escape(config_file) + "\n"
            "Environment=PVE_ENABLED=1\nEnvironment=PVE_CONFIG_LOADED=1\nEnvironment=PVE_ENV_PRELOADED=1\n"
            "Environment=PVE_CONFIG_FILE=" + _unit_escape(config_file) + "\nEnvironment=SENDER_PROFILE=full\n"
            + ("Environment=PYTHONIOENCODING=utf-8\n" if compiled else "") + "ExecStart=" + _unit_escape(python) + ("\n" if compiled else " -m hwmonitor_mqtt.agents.agent_sender_async\n")
            + "Restart=on-failure\nRestartSec=5\n\n[Install]\nWantedBy=multi-user.target\n")


def snapshot_unit_text(python=f"{SERVICE_VENV}/bin/python"):
    return ("[Unit]\nDescription=Collect local PVE snapshot\n\n[Service]\nType=oneshot\n"
            "User=root\nGroup=" + READER_GROUP + "\nRuntimeDirectory=hwmonitor-mqtt-pve\n"
            "RuntimeDirectoryMode=0750\nRuntimeDirectoryPreserve=yes\nWorkingDirectory=" + _unit_escape(APP_ROOT)
            + "\nExecStart=" + _unit_escape(python) + " -E -s -m hwmonitor_mqtt.platforms.pve_snapshot\n")


def snapshot_timer_text():
    return ("[Unit]\nDescription=Refresh local PVE snapshot\n\n[Timer]\n"
            "OnBootSec=15s\nOnUnitInactiveSec=30s\nUnit=" + SNAPSHOT_SERVICE
            + "\n\n[Install]\nWantedBy=timers.target\n")


def local_readiness(config_file=DEFAULT_CONFIG):
    from hwmonitor_mqtt.platforms.pve_bootstrap import PveSetupError, _read_values
    try:
        values = _read_values(Path(config_file))
        import re
        if any(not re.fullmatch(r"[A-Za-z0-9_.-]{1,128}", values.get(key, ""))
               for key in ("PVE_NODE", "PVE_CLUSTER_ID")):
            return "PVE local identity is unconfigured"
        import socket
        if values["PVE_NODE"] != socket.gethostname().split(".", 1)[0]:
            return "PVE_NODE differs from the local node; correct PVE_NODE before installation"
        return None
    except (OSError, ValueError, PveSetupError):
        return "PVE local identity is unreadable"


def _ensure_reader_group(run):
    try:
        grp.getgrnam(READER_GROUP)
    except KeyError:
        if run(["/usr/sbin/groupadd", "--system", READER_GROUP], check=False).returncode:
            raise RuntimeError("cannot create PVE reader group")


def _verify_snapshot(config_file):
    from hwmonitor_mqtt.clients.pve_snapshot import LocalPveConfig, PveSnapshotClient
    from hwmonitor_mqtt.platforms.pve_bootstrap import _read_values
    values = _read_values(Path(config_file))
    client = PveSnapshotClient(LocalPveConfig(values["PVE_CLUSTER_ID"], values["PVE_NODE"]), SNAPSHOT_PATH)
    client.resources()
    client.disks_list()


def _activate_local(project_root, python, config_file, systemctl, run):
    _ensure_reader_group(run)
    directory = Path(SYSTEMD_DIR)
    directory.mkdir(parents=True, exist_ok=True)
    units = {SERVICE_NAME: unit_text(project_root, python, config_file),
             SNAPSHOT_SERVICE: snapshot_unit_text(python), SNAPSHOT_TIMER: snapshot_timer_text()}
    previous = {}
    for name in units:
        path = directory / name
        _validate_trusted_path(path)
        if path.is_symlink() or path.exists() and not path.is_file():
            raise RuntimeError("untrusted systemd unit")
        previous[name] = path.read_bytes() if path.exists() else None
    def state(name):
        if previous[name] is None:
            return False, False
        active = run([systemctl, "is-active", "--quiet", name], check=False).returncode
        enabled = run([systemctl, "is-enabled", "--quiet", name], check=False).returncode
        if active not in (0, 3) or enabled not in (0, 1):
            raise RuntimeError("cannot determine previous PVE service state")
        return active == 0, enabled == 0

    timer_active, timer_enabled = state(SNAPSHOT_TIMER)
    sender_active, sender_enabled = state(SERVICE_NAME)
    sender_touched = timer_touched = False
    try:
        if timer_active and run([systemctl, "stop", SNAPSHOT_TIMER], check=False).returncode:
            raise RuntimeError("cannot stop PVE snapshot timer")
        for name, content in units.items():
            (directory / name).write_text(content)
        for args in ([systemctl, "daemon-reload"], [systemctl, "start", SNAPSHOT_SERVICE]):
            if run(args, check=False).returncode:
                raise RuntimeError("PVE snapshot startup failed")
        _verify_snapshot(config_file)
        for args in ([systemctl, "enable", "--now", SNAPSHOT_TIMER],
                     [systemctl, "enable", SERVICE_NAME], [systemctl, "restart", SERVICE_NAME],
                     [systemctl, "is-active", "--quiet", SERVICE_NAME]):
            if args[-1] == SNAPSHOT_TIMER:
                timer_touched = True
            if args[1] in ("enable", "restart") and args[-1] == SERVICE_NAME:
                sender_touched = True
            if run(args, check=False).returncode:
                raise RuntimeError("PVE sender startup failed")
        return 0
    except (OSError, RuntimeError, PveError, KeyError, ValueError):
        checks = []
        for name, touched in ((SNAPSHOT_TIMER, timer_touched), (SERVICE_NAME, sender_touched)):
            if touched and previous[name] is None:
                checks.append(run([systemctl, "disable", "--now", name], check=False).returncode)
        for name, content in previous.items():
            path = directory / name
            if content is None:
                path.unlink(missing_ok=True)
            else:
                path.write_bytes(content)
        checks.append(run([systemctl, "daemon-reload"], check=False).returncode)
        restore = []
        if previous[SNAPSHOT_TIMER] is not None and (timer_touched or timer_active):
            restore.append((SNAPSHOT_TIMER, timer_active, timer_enabled))
        if sender_touched and previous[SERVICE_NAME] is not None:
            restore.append((SERVICE_NAME, sender_active, sender_enabled))
        for name, active, enabled in restore:
            checks.append(run([systemctl, "enable" if enabled else "disable", name], check=False).returncode)
            checks.append(run([systemctl, "restart" if active else "stop", name], check=False).returncode)
        if any(checks):
            raise RuntimeError("PVE service recovery failed")
        return 1


def _normalize_tree_permissions(root):
    root = Path(root)
    if root.is_symlink():
        raise RuntimeError("installer runtime path must not be a symbolic link")
    root.chmod(0o755)
    for directory, subdirs, files in os.walk(root, topdown=True, followlinks=False):
        current = Path(directory)
        for name in tuple(subdirs):
            path = current / name
            if path.is_symlink():
                subdirs.remove(name)
            else:
                path.chmod(0o755)
        for name in files:
            path = current / name
            if path.is_symlink():
                continue
            mode = stat.S_IMODE(path.stat().st_mode)
            path.chmod(0o755 if mode & 0o111 else 0o644)


def install(project_root, config_file=DEFAULT_CONFIG, run=subprocess.run, which=shutil.which, geteuid=os.geteuid):
    config_file = Path(config_file).resolve()
    compiled = "__compiled__" in globals()
    source_path = Path(project_root, "sender") if compiled else Path(project_root, "hwmonitor_mqtt", "agents", "agent_sender_async.py")
    if not source_path.is_file() or source_path.is_symlink():
        raise RuntimeError("sender executable is missing" if compiled else "sender source is missing")
    systemctl = which("systemctl")
    if not systemctl:
        raise RuntimeError("systemd is unavailable")
    python3 = which("python3")
    if not compiled and not python3:
        raise RuntimeError("python3 is unavailable")
    if geteuid() != 0:
        raise RuntimeError("run the native PVE installer with sudo")
    _validate_executable(systemctl)
    if not compiled:
        _validate_executable(python3)
    for path in (Path(project_root), Path(APP_ROOT).parent, Path(APP_ROOT), Path(SERVICE_VENV), Path(SYSTEMD_DIR)):
        _validate_trusted_path(path)
    if not compiled:
        _validate_trusted_tree(Path(project_root) / "hwmonitor_mqtt")
    if not compiled and (Path(APP_ROOT).exists() or Path(SERVICE_VENV).exists()):
        raise RuntimeError("existing PVE runtime requires source upgrade")
    if not compiled:
        from hwmonitor_mqtt.platforms.pve_bootstrap import PveSetupError, prepare_local
        try:
            prepare_local(config_file)
        except PveSetupError as error:
            raise RuntimeError(str(error)) from None
    validation = readiness(config_file) if compiled else local_readiness(config_file)
    if validation and validation != "PVE configuration is unconfigured":
        raise RuntimeError(validation)
    docker = which("docker")
    if docker:
        _validate_executable(docker)
        existing = run([docker, "inspect", "sys-agent"], check=False, capture_output=True, text=True)
        if existing.returncode == 0:
            raise RuntimeError("sys-agent container exists; stop/remove it before installing the native PVE sender")
        if "No such object" not in (getattr(existing, "stderr", "") or ""):
            raise RuntimeError("could not verify that no sys-agent container is running")
    if compiled:
        return install_compiled(project_root, config_file, systemctl, run)
    source = Path(project_root).resolve() / "hwmonitor_mqtt"
    app = Path(APP_ROOT)
    install_root = app.parent
    if install_root.is_symlink():
        raise RuntimeError("installer runtime path must not be a symbolic link")
    install_root.mkdir(parents=True, exist_ok=True)
    install_root.chmod(0o755)
    package = app / "hwmonitor_mqtt"
    shutil.copytree(source, package, dirs_exist_ok=True, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    _normalize_tree_permissions(app)
    venv_python = Path(SERVICE_VENV) / "bin" / "python"
    for args in ([python3, "-I", "-m", "venv", SERVICE_VENV],
                 [str(venv_python), "-I", "-m", "pip", "install", "psutil>=7", "paho-mqtt>=2.1", "python-dotenv>=1.1"]):
        result = run(args, check=False, env={"PATH": "/usr/sbin:/usr/bin:/sbin:/bin", "LANG": "C.UTF-8", "HOME": "/root"})
        if result.returncode:
            return result.returncode
    _normalize_tree_permissions(Path(SERVICE_VENV))
    return _activate_local(project_root, str(venv_python), config_file, systemctl, run)



def install_compiled(project_root, config_file, systemctl, run):
    app = Path(APP_ROOT)
    unit = Path(SYSTEMD_DIR) / SERVICE_NAME
    if app.is_symlink() or app.parent.is_symlink() or unit.is_symlink() or unit.parent.is_symlink():
        raise RuntimeError("installer runtime or unit path must not be a symbolic link")
    app.parent.mkdir(parents=True, exist_ok=True)
    app.parent.chmod(0o755)
    app.mkdir(parents=True, exist_ok=True)
    app.chmod(0o755)
    unit.parent.mkdir(parents=True, exist_ok=True)
    target = app / "sender"
    _validate_trusted_path(target)
    if target.is_symlink():
        raise RuntimeError("sender executable must not be a symbolic link")
    existed = unit.exists()
    active_status = run([systemctl, "is-active", "--quiet", SERVICE_NAME], check=False).returncode
    enabled_status = run([systemctl, "is-enabled", "--quiet", SERVICE_NAME], check=False).returncode
    if active_status not in (0, 3, 4) or enabled_status not in (0, 1, 4) or existed and 4 in (active_status, enabled_status):
        raise RuntimeError("cannot determine existing service state")
    active, enabled = active_status == 0, enabled_status == 0
    dropdir = unit.parent / (unit.name + ".d")
    override = dropdir / "90-hwmonitor-sender.conf"
    if dropdir.is_symlink() or override.is_symlink():
        raise RuntimeError("installer override path must not be a symbolic link")
    legacy_override = "[Service]\nExecStart=\nExecStart=" + _unit_escape(target) + "\nWorkingDirectory=" + _unit_escape(app) + "\n"
    expected_override = legacy_override + "Environment=PYTHONIOENCODING=utf-8\n"
    if override.exists() and override.read_text() not in (legacy_override, expected_override):
        raise RuntimeError("unrecognized existing sender override")
    if existed:
        current_unit = unit.read_text()
        if "Description=Hardware Monitor MQTT sender for Proxmox VE" not in current_unit or not ("hwmonitor_mqtt.agents.agent_sender_async" in current_unit or str(target) in current_unit):
            raise RuntimeError("unrecognized existing sender service")
    backup = Path(tempfile.mkdtemp(prefix="sender-rollback-", dir=app.parent))
    if target.exists():
        shutil.copy2(target, backup / "sender")
    if existed:
        shutil.copy2(unit, backup / "unit")
    if override.exists():
        shutil.copy2(override, backup / "override")
    started = False
    try:
        shutil.copy2(Path(project_root) / "sender", backup / "new")
        (backup / "new").chmod(0o755)
        os.replace(backup / "new", target)
        if existed:
            dropdir.mkdir(mode=0o755, exist_ok=True)
            dropdir.chmod(0o755)
            override.write_text(expected_override)
        else:
            unit.write_text(unit_text(project_root, str(target), config_file, compiled=True))
        for args in ([systemctl, "daemon-reload"],
                     *([[systemctl, "enable", SERVICE_NAME]] if not existed else []),
                     [systemctl, "restart", SERVICE_NAME]):
            if "restart" in args:
                started = True
            if run(args, check=False).returncode:
                raise RuntimeError("compiled sender startup failed")
        effective = run([systemctl, "show", "--property=ExecStart", "--value", SERVICE_NAME], check=False, capture_output=True, text=True)
        if effective.returncode or "path=" + str(target) not in effective.stdout:
            raise RuntimeError("compiled sender is not the effective service command")
        previous_pid = None
        for _ in range(3):
            time.sleep(1)
            if run([systemctl, "is-active", "--quiet", SERVICE_NAME], check=False).returncode:
                raise RuntimeError("compiled sender did not remain active")
            status = run([systemctl, "show", "--property=MainPID", "--value", SERVICE_NAME], check=False, capture_output=True, text=True)
            pid = status.stdout.strip()
            if status.returncode or not pid.isdigit() or int(pid) == 0 or previous_pid is not None and pid != previous_pid:
                raise RuntimeError("compiled sender did not remain stable")
            previous_pid = pid
        from hwmonitor_mqtt.platforms.install import commit_cancellation
        commit_cancellation()
    except (OSError, RuntimeError, KeyboardInterrupt) as failure:
        try:
            preliminary = []
            if not existed and started:
                preliminary.append(run([systemctl, "stop", SERVICE_NAME], check=False).returncode)
            if not existed and unit.exists():
                preliminary.append(run([systemctl, "disable", SERVICE_NAME], check=False).returncode)
            if (backup / "sender").exists():
                shutil.copy2(backup / "sender", backup / "restore-sender")
                os.replace(backup / "restore-sender", target)
            else:
                target.unlink(missing_ok=True)
            if (backup / "unit").exists():
                shutil.copy2(backup / "unit", unit.with_name(unit.name + ".restore"))
                os.replace(unit.with_name(unit.name + ".restore"), unit)
            else:
                unit.unlink(missing_ok=True)
            if (backup / "override").exists():
                shutil.copy2(backup / "override", override)
            else:
                override.unlink(missing_ok=True)
            rollback = [[systemctl, "daemon-reload"]]
            if existed:
                rollback.append([systemctl, "restart" if active else "stop", SERVICE_NAME])
            results = preliminary + [run(args, check=False).returncode for args in rollback]
            if any(results):
                raise RuntimeError("service recovery failed")
        except (OSError, RuntimeError, KeyboardInterrupt) as error:
            raise RuntimeError(f"sender startup and rollback failed; backup retained at {backup}") from error
        shutil.rmtree(backup)
        if isinstance(failure, KeyboardInterrupt):
            raise
        return 1
    try:
        shutil.rmtree(backup)
    except OSError as error:
        print(f"sender installed; backup cleanup failed at {backup}: {error}", file=sys.stderr)
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project-root", required=True)
    parser.add_argument("--config-file", default=DEFAULT_CONFIG)
    args = parser.parse_args(argv)
    try:
        return install(args.project_root, args.config_file)
    except RuntimeError as error:
        print(str(error))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
