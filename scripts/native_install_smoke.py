#!/usr/bin/env python3
"""Run the native PVE installer against an isolated local MQTT broker."""

import argparse
import grp
import hashlib
import stat
import traceback
import json
import os
import pwd
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import uuid
from threading import Event
from queue import Queue, Empty

REPO = Path(__file__).resolve().parents[1]
SMOKE_PARENT = Path("/var/lib")
sys.path.insert(0, str(REPO))

from paho.mqtt import client as mqtt
from hwmonitor_mqtt.platforms.installers import pve


def pvesh_fixture(token):
    if not token.isalnum():
        raise ValueError("invalid fixture token")
    return f'''#!/bin/sh
# hwmonitor-mqtt-smoke-{token}
[ "$(/usr/bin/id -u)" = 0 ] && [ "$(/usr/bin/id -g)" = 0 ] || exit 1
node=$(/bin/hostname -s) || exit 1
case "$*" in
  "--noproxy get /cluster/resources --output-format json --type vm") printf '[{{"node":"%s","type":"qemu","vmid":987654,"name":"smoke-{token}","status":"running"}}]\\n' "$node" ;;
  "--noproxy get /nodes/$node/disks/list --output-format json") printf '%s\\n' '[{{"devpath":"/dev/smoke-fixture"}}]' ;;
  "--noproxy get /nodes/$node/disks/smart --output-format json --disk /dev/smoke-fixture") printf '%s\\n' '{{"health":"PASSED","attributes":[]}}' ;;
  *) exit 1 ;;
esac
'''


def create_smoke_root():
    try:
        pve._validate_trusted_path(SMOKE_PARENT)
        info = SMOKE_PARENT.lstat()
        if not stat.S_ISDIR(info.st_mode):
            raise OSError("not a directory")
    except (OSError, RuntimeError):
        try:
            info = SMOKE_PARENT.lstat()
            details = f"uid={info.st_uid} mode={stat.S_IMODE(info.st_mode):04o}"
        except OSError:
            details = "unavailable"
        raise RuntimeError(f"untrusted smoke parent {SMOKE_PARENT}: {details}") from None
    return Path(tempfile.mkdtemp(prefix="hwmonitor-mqtt-smoke-", dir=SMOKE_PARENT))


def copy_fixture_source(source, destination):
    shutil.copytree(source, destination, symlinks=True,
                    ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    pve._normalize_tree_permissions(destination)


def create_fixture(path, token):
    data = pvesh_fixture(token).encode()
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o755)
    try:
        with os.fdopen(fd, 'wb') as output:
            output.write(data)
            output.flush()
            os.fchmod(output.fileno(), 0o755)
            os.fsync(output.fileno())
    except BaseException:
        Path(path).unlink(missing_ok=True)
        raise
    return hashlib.sha256(data).hexdigest()


def remove_fixture(path, digest):
    path = Path(path)
    info = path.lstat()
    if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid()
            or hashlib.sha256(path.read_bytes()).hexdigest() != digest):
        return False
    path.unlink()
    return True


def preflight_fixture(paths, group_exists, run=subprocess.run):
    if any(os.path.lexists(path) for path in paths):
        raise RuntimeError("refusing existing PVE or smoke resources")
    if group_exists:
        raise RuntimeError("refusing existing PVE snapshot reader group")
    for name in (pve.SERVICE_NAME, pve.SNAPSHOT_SERVICE, pve.SNAPSHOT_TIMER):
        result = run(["systemctl", "show", "--property=LoadState", "--value", name],
                     capture_output=True, text=True, check=False, timeout=10)
        if result.returncode or result.stdout.strip() != "not-found":
            raise RuntimeError("refusing existing or unknown PVE systemd unit")


def remove_snapshot_directory(path, gid):
    path = Path(path)
    if not os.path.lexists(path):
        return
    info = path.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != 0 or info.st_gid != gid:
        raise RuntimeError("untrusted snapshot directory during cleanup")
    entries = list(path.iterdir())
    if any(item.name != 'snapshot.json' for item in entries):
        raise RuntimeError("unexpected snapshot directory contents")
    for item in entries:
        info = item.lstat()
        if not stat.S_ISREG(info.st_mode) or info.st_uid != 0 or info.st_gid != gid:
            raise RuntimeError("untrusted snapshot during cleanup")
        item.unlink()
    path.rmdir()


def validate_owned_units(directory, hashes):
    for name, digest in hashes.items():
        path = Path(directory) / name
        if not os.path.lexists(path):
            continue
        if path.is_symlink() or not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest() != digest:
            raise RuntimeError(f"smoke unit changed during cleanup: {name}")


def fixture_metric_matches(topic, payload, retained, token, now):
    if retained or topic != f"sys/agents/{socket.gethostname()}/metrics":
        return False
    try:
        metric = json.loads(payload)
        guests = metric['virtualization']['guests']
        smart = metric['disk_smart']
        disk = smart['devices']['/dev/smoke-fixture']
        timestamps = (metric['ts'], metric['virtualization']['observed_at'], smart['observed_at'])
        return (bool(metric['system']) and bool(metric['cpu'])
                and metric['virtualization']['status'] == 'ok'
                and any(guest['name'] == f'smoke-{token}' and guest['vmid'] == 987654 for guest in guests)
                and not smart['partial'] and disk['critical_metrics']['overall_passed'] is True
                and all(type(value) is int and now - 120 <= value <= now + 5 for value in timestamps))
    except (KeyError, TypeError, ValueError):
        return False


def wait_for_fixture_metric(messages, token, timeout, *, monotonic=time.monotonic):
    deadline = monotonic() + timeout
    while True:
        remaining = deadline - monotonic()
        if remaining <= 0:
            raise TimeoutError("no fresh PVE fixture metric arrived")
        try:
            topic, payload, retained = messages.get(timeout=remaining)
        except Empty:
            raise TimeoutError("no fresh PVE fixture metric arrived") from None
        if fixture_metric_matches(topic, payload, retained, token, int(time.time())):
            return topic


def cleanup_owned_units(directory, hashes, timer_name, snapshot_name, sender_name, *, run=subprocess.run):
    directory = Path(directory)
    errors = []
    for name, parent, static, timer in (
        (timer_name, "timers.target.wants", False, True),
        (snapshot_name, "multi-user.target.wants", True, False),
        (sender_name, "multi-user.target.wants", False, False),
    ):
        unit, link = directory / name, directory / parent / name
        digest = hashes[name]
        try:
            if digest is None:
                if os.path.lexists(unit) or os.path.lexists(link):
                    raise RuntimeError("unidentified smoke unit retained")
                continue
            validate_owned_units(directory, {name: digest})
            if os.path.lexists(link) and (not link.is_symlink() or link.resolve() != unit):
                raise RuntimeError("unidentified smoke unit link retained")
            error = cleanup_systemd_service(name, unit, link, run=run, static=static, timer=timer, known=True)
            if error:
                raise RuntimeError(error)
        except (OSError, RuntimeError) as error:
            errors.append(f"{name}: {error}")
    return errors


def verify_reader_access(snapshot, gid):
    snapshot = Path(snapshot)
    directory = snapshot.parent.lstat()
    info = snapshot.lstat()
    if (not stat.S_ISDIR(directory.st_mode) or directory.st_uid != 0 or directory.st_gid != gid
            or stat.S_IMODE(directory.st_mode) != 0o750 or not stat.S_ISREG(info.st_mode)
            or info.st_uid != 0 or info.st_gid != gid or stat.S_IMODE(info.st_mode) != 0o640):
        raise RuntimeError("invalid snapshot ownership or permissions")
    nobody = pwd.getpwnam('nobody')
    code = ("import sys; p=sys.argv[1]; allow=sys.argv[2]=='yes'; "
            "exec(\"try:\\n f=open(p,'rb'); f.read(1); f.close()\\nexcept PermissionError:\\n assert not allow\\nelse:\\n assert allow\\ntry:\\n open(p,'wb')\\nexcept PermissionError:\\n pass\\nelse:\\n raise AssertionError('snapshot writable')\\ntry:\\n __import__('os').unlink(p)\\nexcept PermissionError:\\n pass\\nelse:\\n raise AssertionError('snapshot removable')\")")
    for allowed in (True, False):
        groups = ["--groups", str(gid)] if allowed else ["--clear-groups"]
        subprocess.run(["/usr/bin/setpriv", "--reuid", str(nobody.pw_uid), "--regid", str(nobody.pw_gid),
                        *groups, "/usr/bin/python3", "-I", "-c", code, str(snapshot),
                        'yes' if allowed else 'no'], check=True, capture_output=True, timeout=10)



def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run", action="store_true", help="required: install a temporary systemd service as root")
    parser.add_argument("--timeout", type=int, default=90, help="seconds to wait for a real MQTT message")
    args = parser.parse_args(argv)
    if not args.run:
        parser.error("native installation changes /var/lib and /etc/systemd/system; pass --run to confirm an isolated smoke run")
    if os.geteuid() != 0:
        parser.error("run this smoke check as root (for example, with sudo)")
    if args.timeout < 1:
        parser.error("--timeout must be positive")
    if not shutil.which("systemctl") or not Path("/usr/bin/python3").exists() or not shutil.which("mosquitto"):
        parser.error("systemctl, /usr/bin/python3, and mosquitto are required")
    pve._validate_executable("/usr/bin/python3")
    systemd_dir = Path("/etc/systemd/system")
    fixture_path = Path("/usr/bin/pvesh")
    snapshot_dir = Path("/run/hwmonitor-mqtt-pve")
    try:
        grp.getgrnam(pve.READER_GROUP)
        group_exists = True
    except KeyError:
        group_exists = False
    preflight_fixture([fixture_path, Path("/usr/sbin/pvesh"), Path("/etc/pve"),
                       Path(pve.INSTALL_ROOT), snapshot_dir,
                       *(systemd_dir / name for name in (pve.SERVICE_NAME, pve.SNAPSHOT_SERVICE, pve.SNAPSHOT_TIMER)),
                       systemd_dir / "timers.target.wants" / pve.SNAPSHOT_TIMER,
                       systemd_dir / "multi-user.target.wants" / pve.SERVICE_NAME,
                       systemd_dir / "multi-user.target.wants" / pve.SNAPSHOT_SERVICE], group_exists)

    old_umask = os.umask(0o077)
    exit_code = 0
    install_root = None
    token = uuid.uuid4().hex
    service_name = f"hwmonitor-mqtt-smoke-{token}.service"
    snapshot_service = f"hwmonitor-mqtt-smoke-{token}-snapshot.service"
    snapshot_timer = f"hwmonitor-mqtt-smoke-{token}-snapshot.timer"
    owned_units = {service_name: None, snapshot_service: None, snapshot_timer: None}
    fixture_digest = None
    group_gid = None
    group_created = False
    def save_manifest():
        (install_root / 'owned-resources.json').write_text(json.dumps({
            'fixture': str(fixture_path), 'fixture_sha256': fixture_digest,
            'snapshot_directory': str(snapshot_dir), 'group': pve.READER_GROUP, 'group_gid': group_gid,
            'units': owned_units,
        }, indent=2) + '\n')
    success = False
    broker = None
    old_installer_paths = (pve.INSTALL_ROOT, pve.APP_ROOT, pve.SERVICE_VENV, pve.SERVICE_NAME, pve.SYSTEMD_DIR, pve.SNAPSHOT_SERVICE, pve.SNAPSHOT_TIMER)
    try:
        install_root = create_smoke_root()
        fixture_digest = create_fixture(fixture_path, token)
        save_manifest()
        group_result = subprocess.run(["/usr/sbin/groupadd", "--system", pve.READER_GROUP], check=False)
        if group_result.returncode:
            raise RuntimeError("cannot create smoke reader group")
        group_created = True
        group_gid = grp.getgrnam(pve.READER_GROUP).gr_gid
        save_manifest()
        config = install_root / "pve.env"
        config.write_text("")
        config.chmod(0o600)
        project_root = install_root / "project"
        copy_fixture_source(REPO / "hwmonitor_mqtt", project_root / "hwmonitor_mqtt")
        install_root.chmod(0o755)
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
        broker_config = install_root / "mosquitto.conf"
        broker_config.write_text(f"listener {port} 127.0.0.1\nallow_anonymous true\npersistence false\n")
        broker = subprocess.Popen(["mosquitto", "-c", str(broker_config)], stdout=subprocess.DEVNULL,
                                  stderr=subprocess.DEVNULL)
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            if broker.poll() is not None:
                raise RuntimeError("local mosquitto broker exited during startup")
            try:
                with socket.create_connection(("127.0.0.1", port), timeout=0.2):
                    break
            except OSError:
                time.sleep(0.1)
        else:
            raise RuntimeError("local mosquitto broker did not start")

        runtime_root = install_root / "runtime"
        pve.INSTALL_ROOT = str(runtime_root)
        pve.APP_ROOT = str(runtime_root / "app")
        pve.SERVICE_VENV = str(runtime_root / "venv")
        pve.SERVICE_NAME = service_name
        pve.SNAPSHOT_SERVICE = snapshot_service
        pve.SNAPSHOT_TIMER = snapshot_timer

        def run(command, check=False, **kwargs):
            if (Path(command[0]).name == "systemctl" and command[1:2] == ["daemon-reload"]
                    and owned_units[service_name] is None and (systemd_dir / service_name).is_file()):
                unit = systemd_dir / service_name
                unit_text = unit.read_text().replace(
                    "[Install]", f"Environment=BROKER_HOST=127.0.0.1\nEnvironment=BROKER_PORT={port}\n"
                    "Environment=PUBLISH_INTERVAL_SEC=1\n\n[Install]"
                )
                unit.write_text(unit_text)
                for name in owned_units:
                    path = systemd_dir / name
                    if path.is_file() and owned_units[name] is None:
                        with path.open("a") as output:
                            output.write(f"\n# hwmonitor-mqtt-smoke-{token}\n")
                        owned_units[name] = hashlib.sha256(path.read_bytes()).hexdigest()
                save_manifest()
            return subprocess.run(command, check=check, timeout=120, **kwargs)

        pve.SYSTEMD_DIR = str(systemd_dir)
        subscriber = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=f"smoke-{os.getpid()}")
        messages = Queue()
        subscribed = Event()
        subscriber.on_connect = lambda client, userdata, flags, reason, properties=None: client.subscribe("sys/agents/+/metrics")
        subscriber.on_subscribe = lambda client, userdata, mid, reason, properties=None: subscribed.set()
        subscriber.on_message = lambda client, userdata, message: messages.put((message.topic, message.payload, message.retain))
        subscriber.connect("127.0.0.1", port, keepalive=30)
        subscriber.loop_start()
        try:
            if not subscribed.wait(10):
                raise RuntimeError("local MQTT subscriber did not connect")
            which = lambda name: None if name == "docker" else "/usr/bin/python3" if name == "python3" else shutil.which(name)
            result = pve.install(project_root, config_file=config, run=run, which=which)
            if result:
                raise RuntimeError(f"native installer exited with status {result}")
            unit_text = (systemd_dir / service_name).read_text()
            if "DynamicUser=yes" not in unit_text or stat_mode(config) != 0o600:
                raise RuntimeError("installed service or private fixture configuration has unsafe permissions")
            service_python = runtime_root / "venv" / "bin" / "python"
            version = subprocess.run(
                [str(service_python), "-c", "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')"],
                check=True, capture_output=True, text=True, timeout=10,
            ).stdout.strip()
            expected_version = subprocess.run(["/usr/bin/python3", "-I", "-c", "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')"], check=True, capture_output=True, text=True, timeout=10).stdout.strip()
            if version != expected_version:
                raise RuntimeError(f"native venv uses Python {version}; expected {expected_version}")
            subprocess.run([str(service_python), "-m", "pip", "--version"],
                           check=True, capture_output=True, text=True, timeout=10)
            subprocess.run(["systemctl", "is-active", "--quiet", snapshot_timer], check=True, timeout=10)
            snapshot_result = subprocess.run(["systemctl", "show", "--property=Result", "--value", snapshot_service],
                                             check=True, capture_output=True, text=True, timeout=10)
            if snapshot_result.stdout.strip() != 'success':
                raise RuntimeError("snapshot service did not succeed")
            verify_reader_access(snapshot_dir / 'snapshot.json', group_gid)
            try:
                topic = wait_for_fixture_metric(messages, token, args.timeout)
            except TimeoutError as error:
                status = subprocess.run(["systemctl", "status", service_name, "--no-pager"],
                                        capture_output=True, text=True, timeout=10)
                raise RuntimeError(f"no fresh PVE fixture metric arrived; systemd status: {status.stdout}\n{status.stderr}") from error
            subprocess.run(["systemctl", "is-active", "--quiet", service_name], check=True, timeout=10)
            subprocess.run(["systemctl", "is-active", "--quiet", snapshot_timer], check=True, timeout=10)
            print(f"native service published fresh PVE and SMART fixture data on {topic}")
        finally:
            subscriber.loop_stop()
            subscriber.disconnect()
        success = True
    except BaseException:
        if install_root is not None:
            (install_root / 'failure.log').write_text(traceback.format_exc())
        raise
    finally:
        if install_root is not None:
            wants = systemd_dir / "multi-user.target.wants"
            unit = systemd_dir / service_name
            link = wants / service_name
            try:
                errors = cleanup_owned_units(systemd_dir, owned_units, snapshot_timer, snapshot_service, service_name)
                if errors:
                    raise RuntimeError("; ".join(errors) + "; snapshot, group and fixture retained")
                if group_created and group_gid is None:
                    raise RuntimeError("created reader group has unknown gid; retained for inspection")
                if group_gid is not None:
                    remove_snapshot_directory(snapshot_dir, group_gid)
                    group = grp.getgrnam(pve.READER_GROUP)
                    if group.gr_gid != group_gid or group.gr_mem or any(user.pw_gid == group_gid for user in pwd.getpwall()):
                        raise RuntimeError("reader group changed during smoke")
                    if subprocess.run(["/usr/sbin/groupdel", pve.READER_GROUP], check=False).returncode:
                        raise RuntimeError("cannot remove smoke reader group")
                if fixture_digest and not remove_fixture(fixture_path, fixture_digest):
                    raise RuntimeError("pvesh fixture changed during smoke")
                if success:
                    shutil.rmtree(install_root)
            except (OSError, RuntimeError, KeyError) as error:
                exit_code = 1
                with (install_root / 'failure.log').open('a') as output:
                    output.write(f"Smoke cleanup failed: {error}\n")
                print(f"Smoke cleanup failed: {error}; inspect {install_root}", file=sys.stderr)
        try:
            if broker is not None:
                broker.terminate()
                broker.wait(timeout=5)
        except ProcessLookupError:
            pass
        except subprocess.TimeoutExpired:
            broker.kill()
            broker.wait()
        finally:
            os.umask(old_umask)
            pve.INSTALL_ROOT, pve.APP_ROOT, pve.SERVICE_VENV, pve.SERVICE_NAME, pve.SYSTEMD_DIR, pve.SNAPSHOT_SERVICE, pve.SNAPSHOT_TIMER = old_installer_paths
    return exit_code


def stat_mode(path):
    return Path(path).stat().st_mode & 0o777


def cleanup_smoke_install(service_name, unit, wants_link, install_root, run=subprocess.run):
    error = cleanup_systemd_service(service_name, unit, wants_link, run=run)
    if error:
        return 1, error
    try:
        shutil.rmtree(install_root)
    except OSError as error:
        return 1, f"could not remove the smoke files: {error}"
    return 0, None


def cleanup_systemd_service(service_name, unit, wants_link, run=subprocess.run, *, static=False, timer=False, known=False):
    unit, wants_link = Path(unit), Path(wants_link)
    if not known and not os.path.lexists(unit) and not os.path.lexists(wants_link):
        return None

    def systemctl(*args, timeout=20):
        command = ["systemctl", *args]
        try:
            return run(command, check=False, capture_output=True, text=True, timeout=timeout)
        except (OSError, subprocess.TimeoutExpired):
            return None

    disabled = None if static else systemctl("disable", service_name)
    stopped = systemctl("stop", service_name)
    if stopped is None or stopped.returncode:
        systemctl("kill", "--kill-who=all", "--signal=KILL", service_name)
        systemctl("stop", service_name)

    state = systemctl("show", "--property=ActiveState", "--value", service_name, timeout=10)
    pid = None if timer else systemctl("show", "--property=MainPID", "--value", service_name, timeout=10)
    if (state is None or state.returncode or state.stdout.strip() != "inactive"
            or not timer and (pid is None or pid.returncode or pid.stdout.strip() != "0")):
        active_state = state.stdout.strip() if state is not None else "unknown"
        main_pid = pid.stdout.strip() if pid is not None else "unknown"
        return f"systemd reported ActiveState={active_state}, MainPID={main_pid}"
    if not static and (disabled is None or disabled.returncode):
        if not known or os.path.lexists(unit) or os.path.lexists(wants_link):
            return "systemctl disable failed"
        enabled = systemctl("is-enabled", "--quiet", service_name)
        if enabled is None or enabled.returncode not in (1, 4):
            return "systemctl disable failed"

    try:
        wants_link.unlink(missing_ok=True)
        unit.unlink(missing_ok=True)
    except OSError as error:
        return f"could not remove the unit files: {error}"
    reloaded = systemctl("daemon-reload")
    if reloaded is None or reloaded.returncode:
        return "systemctl daemon-reload failed after unit removal"
    return None


if __name__ == "__main__":
    raise SystemExit(main())
