import os
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace


REPO = Path(__file__).resolve().parents[1]
SMOKE = REPO / "scripts" / "native_install_smoke.py"
sys.path.insert(0, str(REPO))

from scripts.native_install_smoke import cleanup_smoke_install, cleanup_systemd_service


def test_native_smoke_requires_explicit_run_before_touching_systemd(tmp_path):
    result = subprocess.run(
        [sys.executable, str(SMOKE)],
        cwd=tmp_path,
        env={**os.environ, "HOME": str(tmp_path)},
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 2
    assert "--run" in result.stderr
    assert list(tmp_path.iterdir()) == []


def test_failed_disable_preserves_installed_files_and_reports_cleanup_failure(tmp_path):
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    unit = tmp_path / "fixture.service"
    unit.write_text("[Service]\n")
    wants = tmp_path / "multi-user.target.wants"
    wants.mkdir()
    link = wants / unit.name
    link.symlink_to(unit)

    def run(command, **kwargs):
        if command[1] == "disable":
            return SimpleNamespace(returncode=1, stdout="", stderr="fixture failure")
        if command[1] == "stop":
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        if command[1] == "show":
            state = "inactive\n" if command[2] == "--property=ActiveState" else "0\n"
            return SimpleNamespace(returncode=0, stdout=state, stderr="")
        raise AssertionError(f"unexpected systemctl operation: {command}")

    exit_code, error = cleanup_smoke_install("fixture.service", unit, link, tmp_path, run=run)

    assert exit_code == 1
    assert error is not None
    assert "disable" in error
    assert runtime.is_dir()
    assert unit.is_file()
    assert link.is_symlink()


def test_failed_stop_kills_then_verifies_before_removing_service_files(tmp_path):
    unit = tmp_path / "fixture.service"
    unit.write_text("[Service]\n")
    link = tmp_path / "multi-user.target.wants" / unit.name
    link.parent.mkdir()
    link.symlink_to(unit)
    calls = []
    stop_calls = 0

    def run(command, **kwargs):
        nonlocal stop_calls
        calls.append(command[1])
        if command[1] == "stop":
            stop_calls += 1
            return SimpleNamespace(returncode=1 if stop_calls == 1 else 0, stdout="", stderr="")
        if command[1] == "show":
            state = "inactive\n" if command[2] == "--property=ActiveState" else "0\n"
            return SimpleNamespace(returncode=0, stdout=state, stderr="")
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    error = cleanup_systemd_service("fixture.service", unit, link, run=run)

    assert error is None
    assert calls == ["disable", "stop", "kill", "stop", "show", "show", "daemon-reload"]
    assert not unit.exists()
    assert not link.exists()


def test_service_still_active_after_kill_preserves_runtime_and_unit(tmp_path):
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    unit = tmp_path / "fixture.service"
    unit.write_text("[Service]\n")
    link = tmp_path / "multi-user.target.wants" / unit.name
    link.parent.mkdir()
    link.symlink_to(unit)

    def run(command, **kwargs):
        if command[1] == "show":
            state = "active\n" if command[2] == "--property=ActiveState" else "1234\n"
            return SimpleNamespace(returncode=0, stdout=state, stderr="")
        return SimpleNamespace(returncode=1, stdout="", stderr="stop failed")

    exit_code, error = cleanup_smoke_install("fixture.service", unit, link, tmp_path, run=run)

    assert exit_code == 1
    assert error is not None
    assert "MainPID=1234" in error
    assert runtime.is_dir()
    assert unit.is_file()
    assert link.is_symlink()


def fixture_id_program(tmp_path, uid, gid):
    bindir = tmp_path / f'identity-{uid}-{gid}'
    bindir.mkdir(exist_ok=True)
    identity = bindir / 'id'
    identity.write_text(f'#!/bin/sh\ncase "$1" in -u) echo {uid} ;; -g) echo {gid} ;; *) exit 1 ;; esac\n')
    identity.chmod(0o755)
    return identity


def test_pvesh_fixture_accepts_only_fixed_requests(tmp_path):
    from scripts.native_install_smoke import pvesh_fixture
    fixture = tmp_path / 'pvesh'
    identity = fixture_id_program(tmp_path, 0, 0)
    fixture.write_text(pvesh_fixture('proof').replace('/usr/bin/id', str(identity)))
    fixture.chmod(0o755)
    node = subprocess.check_output(['hostname', '-s'], text=True).strip()
    base = [str(fixture), '--noproxy', 'get']
    valid = subprocess.run(base + ['/cluster/resources', '--output-format', 'json', '--type', 'vm'], capture_output=True, text=True)
    assert valid.returncode == 0 and 'smoke-proof' in valid.stdout
    disks = subprocess.run(base + [f'/nodes/{node}/disks/list', '--output-format', 'json'], capture_output=True, text=True)
    assert '/dev/smoke-fixture' in disks.stdout
    smart = subprocess.run(base + [f'/nodes/{node}/disks/smart', '--output-format', 'json', '--disk', '/dev/smoke-fixture'], capture_output=True, text=True)
    assert 'PASSED' in smart.stdout
    assert subprocess.run(base + ['/nodes/remote/disks/list', '--output-format', 'json'], capture_output=True).returncode != 0
    assert subprocess.run([str(fixture), 'delete', '/cluster/resources'], capture_output=True).returncode != 0


def test_fixture_removal_refuses_replaced_file(tmp_path):
    from scripts.native_install_smoke import create_fixture, remove_fixture
    path = tmp_path / 'pvesh'
    digest = create_fixture(path, 'unique')
    path.write_text('changed')
    assert remove_fixture(path, digest) is False
    assert path.read_text() == 'changed'


def test_fixture_creation_never_overwrites_existing_or_symlink(tmp_path):
    import pytest
    from scripts.native_install_smoke import create_fixture
    victim = tmp_path / 'victim'
    victim.write_text('keep')
    path = tmp_path / 'pvesh'
    path.symlink_to(victim)
    with pytest.raises(FileExistsError):
        create_fixture(path, 'unique')
    assert victim.read_text() == 'keep'


def test_metric_requires_fresh_unretained_matching_vm_and_smart():
    import json
    import socket
    from scripts.native_install_smoke import fixture_metric_matches
    now = 1000
    data = {'ts': now, 'system': {'uptime': 1}, 'cpu': {'percent': 2},
            'virtualization': {'status': 'ok', 'observed_at': now, 'guests': [{'name': 'smoke-token', 'vmid': 987654}]},
            'disk_smart': {'observed_at': now, 'partial': False,
                           'devices': {'/dev/smoke-fixture': {'critical_metrics': {'overall_passed': True}}}}}
    topic = f'sys/agents/{socket.gethostname()}/metrics'
    assert fixture_metric_matches(topic, json.dumps(data), False, 'token', now)
    assert not fixture_metric_matches(topic, json.dumps(data), True, 'token', now)
    assert not fixture_metric_matches(topic, json.dumps(data), False, 'wrong', now)
    data['disk_smart']['observed_at'] = now - 121
    assert not fixture_metric_matches(topic, json.dumps(data), False, 'token', now)


def test_preflight_refuses_existing_pve_resource_and_group(tmp_path):
    import pytest
    from scripts.native_install_smoke import preflight_fixture
    resource = tmp_path / 'pvesh'
    resource.symlink_to('missing')
    with pytest.raises(RuntimeError, match='existing'):
        preflight_fixture([resource], False)
    resource.unlink()
    with pytest.raises(RuntimeError, match='group'):
        preflight_fixture([resource], True)


def test_cleanup_refuses_changed_unit(tmp_path):
    import hashlib
    import pytest
    from scripts.native_install_smoke import validate_owned_units
    unit = tmp_path / 'smoke.service'
    unit.write_text('owned')
    digest = hashlib.sha256(b'owned').hexdigest()
    validate_owned_units(tmp_path, {unit.name: digest})
    unit.write_text('changed')
    with pytest.raises(RuntimeError, match='changed'):
        validate_owned_units(tmp_path, {unit.name: digest})
    assert unit.read_text() == 'changed'


def test_timer_cleanup_requires_inactive_before_removal(tmp_path):
    unit = tmp_path / 'smoke.timer'
    unit.write_text('owned')
    link = tmp_path / 'timers.target.wants' / unit.name
    link.parent.mkdir()
    link.symlink_to(unit)
    def run(command, **kwargs):
        if command[1] == 'show':
            return SimpleNamespace(returncode=0, stdout='active\n', stderr='')
        return SimpleNamespace(returncode=0, stdout='', stderr='')
    error = cleanup_systemd_service('smoke.timer', unit, link, run=run, timer=True)
    assert 'ActiveState=active' in error
    assert unit.exists() and link.is_symlink()


def test_static_snapshot_service_cleanup_does_not_disable(tmp_path):
    unit = tmp_path / 'smoke.service'
    unit.write_text('owned')
    link = tmp_path / 'link'
    calls = []
    def run(command, **kwargs):
        calls.append(command[1])
        if command[1] == 'show':
            return SimpleNamespace(returncode=0, stdout='inactive\n' if command[2] == '--property=ActiveState' else '0\n', stderr='')
        return SimpleNamespace(returncode=0, stdout='', stderr='')
    assert cleanup_systemd_service('smoke.service', unit, link, run=run, static=True) is None
    assert 'disable' not in calls
    assert not unit.exists()


def test_fixture_drives_real_snapshot_collector(tmp_path):
    from scripts.native_install_smoke import pvesh_fixture
    from hwmonitor_mqtt.collectors.pve_local import collect_snapshot
    from hwmonitor_mqtt.collectors.smart import _pve_smart_to_json
    fixture = tmp_path / 'pvesh'
    identity = fixture_id_program(tmp_path, 0, 0)
    fixture.write_text(pvesh_fixture('trial').replace('/usr/bin/id', str(identity)))
    fixture.chmod(0o755)
    def runner(args, timeout):
        return subprocess.check_output([str(fixture), *args[1:]], timeout=timeout)
    result = collect_snapshot(runner=runner)
    assert not result['errors']
    assert result['resources'][0]['name'] == 'smoke-trial'
    assert result['disks'][0]['devpath'] == '/dev/smoke-fixture'
    assert _pve_smart_to_json(result['smart']['/dev/smoke-fixture'])['smart_status']['passed'] is True


def test_cleanup_does_not_ignore_dangling_enablement_link(tmp_path):
    unit = tmp_path / 'missing.service'
    link = tmp_path / 'wants' / unit.name
    link.parent.mkdir()
    link.symlink_to(unit)
    def run(command, **kwargs):
        if command[1] == 'show':
            return SimpleNamespace(returncode=0, stdout='inactive\n' if command[2] == '--property=ActiveState' else '0\n', stderr='')
        return SimpleNamespace(returncode=0, stdout='', stderr='')
    assert cleanup_systemd_service(unit.name, unit, link, run=run) is None
    assert not os.path.lexists(link)


def test_reader_access_uses_separate_group_and_no_group_processes(tmp_path, monkeypatch):
    import stat
    from scripts import native_install_smoke as smoke
    snapshot = tmp_path / 'snapshot.json'
    real_lstat = Path.lstat
    def lstat(path):
        if path == tmp_path:
            return SimpleNamespace(st_mode=stat.S_IFDIR | 0o750, st_uid=0, st_gid=1234)
        if path == snapshot:
            return SimpleNamespace(st_mode=stat.S_IFREG | 0o640, st_uid=0, st_gid=1234)
        return real_lstat(path)
    calls = []
    monkeypatch.setattr(Path, 'lstat', lstat)
    monkeypatch.setattr(smoke.pwd, 'getpwnam', lambda name: SimpleNamespace(pw_uid=65534, pw_gid=65534))
    def run(args, **kwargs):
        compile(args[args.index('-c') + 1], '<reader-check>', 'exec')
        calls.append(args)
    monkeypatch.setattr(smoke.subprocess, 'run', run)
    smoke.verify_reader_access(snapshot, 1234)
    assert '--groups' in calls[0] and '1234' in calls[0]
    assert '--clear-groups' in calls[1]


def test_cleanup_continues_after_snapshot_service_failure(tmp_path):
    import hashlib
    from scripts.native_install_smoke import cleanup_owned_units
    names = ['smoke.timer', 'snapshot.service', 'sender.service']
    hashes = {}
    for name in names:
        (tmp_path / name).write_text(name)
        hashes[name] = hashlib.sha256(name.encode()).hexdigest()
    calls = []
    def run(command, **kwargs):
        calls.append(command)
        if command[1] == 'show':
            value = 'failed' if command[-1] == 'snapshot.service' and command[2] == '--property=ActiveState' else ('inactive' if command[2] == '--property=ActiveState' else '0')
            return SimpleNamespace(returncode=0, stdout=value, stderr='')
        return SimpleNamespace(returncode=0, stdout='', stderr='')
    errors = cleanup_owned_units(tmp_path, hashes, *names, run=run)
    assert errors and 'snapshot.service' in ' '.join(errors)
    assert ['systemctl', 'stop', 'sender.service'] in calls
    assert (tmp_path / 'snapshot.service').exists()
    assert not (tmp_path / 'sender.service').exists()


def test_cleanup_skips_changed_unit_but_stops_other_owned_units(tmp_path):
    import hashlib
    from scripts.native_install_smoke import cleanup_owned_units
    names = ['smoke.timer', 'snapshot.service', 'sender.service']
    hashes = {}
    for name in names:
        (tmp_path / name).write_text(name)
        hashes[name] = hashlib.sha256(name.encode()).hexdigest()
    (tmp_path / names[1]).write_text('changed')
    calls = []
    def run(command, **kwargs):
        calls.append(command)
        if command[1] == 'show':
            return SimpleNamespace(returncode=0, stdout='inactive' if command[2] == '--property=ActiveState' else '0', stderr='')
        return SimpleNamespace(returncode=0, stdout='', stderr='')
    errors = cleanup_owned_units(tmp_path, hashes, *names, run=run)
    assert errors and 'snapshot.service' in ' '.join(errors)
    assert not any(command[-1] == 'snapshot.service' for command in calls)
    assert ['systemctl', 'stop', 'sender.service'] in calls
    assert (tmp_path / names[1]).read_text() == 'changed'


def test_known_missing_unit_still_stops_and_checks_live_service(tmp_path):
    from scripts.native_install_smoke import cleanup_owned_units
    names = ['smoke.timer', 'snapshot.service', 'sender.service']
    calls = []
    active = True
    def run(command, **kwargs):
        nonlocal active
        calls.append(command)
        if command[1] == 'stop':
            active = False
        if command[1] == 'show':
            state = ('active' if active else 'inactive') if command[2] == '--property=ActiveState' else ('1234' if active else '0')
            return SimpleNamespace(returncode=0, stdout=state, stderr='')
        return SimpleNamespace(returncode=0, stdout='', stderr='')
    assert cleanup_owned_units(tmp_path, {names[0]: None, names[1]: None, names[2]: 'known-digest'}, *names, run=run) == []
    assert ['systemctl', 'stop', 'sender.service'] in calls
    assert ['systemctl', 'show', '--property=MainPID', '--value', 'sender.service'] in calls
    assert not active


def test_known_missing_unit_with_failed_stop_reports_active(tmp_path):
    from scripts.native_install_smoke import cleanup_owned_units
    names = ['smoke.timer', 'snapshot.service', 'sender.service']
    def run(command, **kwargs):
        if command[1] == 'show':
            return SimpleNamespace(returncode=0, stdout='active' if command[2] == '--property=ActiveState' else '1234', stderr='')
        return SimpleNamespace(returncode=1 if command[1] == 'stop' else 0, stdout='', stderr='')
    errors = cleanup_owned_units(tmp_path, {names[0]: None, names[1]: None, names[2]: 'known-digest'}, *names, run=run)
    assert errors and 'ActiveState=active, MainPID=1234' in errors[0]


def test_wrong_messages_cannot_extend_deadline():
    import pytest
    from scripts.native_install_smoke import wait_for_fixture_metric
    class EndlessQueue:
        def get(self, timeout):
            return 'wrong', b'{}', False
    times = iter([0, 0.5, 1, 1.5, 2])
    with pytest.raises(TimeoutError):
        wait_for_fixture_metric(EndlessQueue(), 'token', 2, monotonic=lambda: next(times))


def test_untrusted_smoke_parent_is_rejected_before_creating_files(tmp_path, monkeypatch):
    import pytest
    from scripts import native_install_smoke as smoke
    monkeypatch.setattr(smoke, 'SMOKE_PARENT', tmp_path)
    monkeypatch.setattr(smoke.tempfile, 'mkdtemp', lambda **kwargs: pytest.fail('unexpected file creation'))
    with pytest.raises(RuntimeError, match='untrusted smoke parent'):
        smoke.create_smoke_root()
    assert list(tmp_path.iterdir()) == []


def test_smoke_root_created_only_after_validating_parent(tmp_path, monkeypatch):
    from scripts import native_install_smoke as smoke
    calls = []
    monkeypatch.setattr(smoke, 'SMOKE_PARENT', tmp_path)
    monkeypatch.setattr(smoke.pve, '_validate_trusted_path', lambda path: calls.append(('validate', Path(path))))
    def make(*, prefix, dir):
        calls.append(('mkdir', Path(dir)))
        path = Path(dir) / (prefix + 'fixture')
        path.mkdir()
        return str(path)
    monkeypatch.setattr(smoke.tempfile, 'mkdtemp', make)
    root = smoke.create_smoke_root()
    assert calls == [('validate', tmp_path), ('mkdir', tmp_path)]
    assert root.parent == tmp_path


def test_fixture_source_copy_removes_group_write_without_following_symlinks(tmp_path):
    import stat
    from scripts.native_install_smoke import copy_fixture_source
    source = tmp_path / 'checkout'
    source.mkdir(mode=0o775)
    source.chmod(0o775)
    (source / 'module.py').write_text('fixture')
    (source / 'module.py').chmod(0o664)
    victim = tmp_path / 'victim'
    victim.write_text('safe')
    (source / 'link').symlink_to(victim)
    target = tmp_path / 'private' / 'hwmonitor_mqtt'
    copy_fixture_source(source, target)
    assert stat.S_IMODE(target.stat().st_mode) == 0o755
    assert stat.S_IMODE((target / 'module.py').stat().st_mode) == 0o644
    assert (target / 'link').is_symlink()
    assert victim.read_text() == 'safe'


def test_cleanup_success_removes_only_owned_runtime(tmp_path):
    runtime = tmp_path / 'owned'
    runtime.mkdir()
    sibling = tmp_path / 'other'
    sibling.mkdir()
    (sibling / 'keep').write_text('safe')
    unit = tmp_path / 'missing.service'
    link = tmp_path / 'missing-link'
    result, error = cleanup_smoke_install('missing.service', unit, link, runtime)
    assert result == 0 and error is None
    assert not runtime.exists()
    assert (sibling / 'keep').read_text() == 'safe'


def test_pvesh_fixture_rejects_nonroot_uid_or_primary_gid(tmp_path):
    from scripts.native_install_smoke import pvesh_fixture
    fixture = tmp_path / 'pvesh'
    command = [str(fixture), '--noproxy', 'get', '/cluster/resources', '--output-format', 'json', '--type', 'vm']
    for uid, gid in ((1000, 0), (0, 1000), (1000, 1000), (0, 0)):
        identity = fixture_id_program(tmp_path, uid, gid)
        fixture.write_text(pvesh_fixture('proof').replace('/usr/bin/id', str(identity)))
        fixture.chmod(0o755)
        result = subprocess.run(command, capture_output=True)
        if (uid, gid) == (0, 0):
            assert result.returncode == 0 and b'smoke-proof' in result.stdout
        else:
            assert result.returncode != 0 and result.stdout == b''
