from pathlib import Path
import subprocess

import pytest

from scripts.source_pve import PveActivation


@pytest.fixture(autouse=True)
def trusted_fixture_paths(monkeypatch):
    monkeypatch.setattr("scripts.source_pve._validate_trusted_path", lambda path: None)
    monkeypatch.setattr("scripts.source_pve._validate_trusted_tree", lambda path: None)
    monkeypatch.setattr("scripts.source_pve._validate_executable", lambda path: None)


@pytest.fixture
def existing_install(tmp_path):
    root, runtime, units = tmp_path / 'root', tmp_path / 'runtime', tmp_path / 'systemd'
    root.mkdir()
    (root / '.env').write_text('private settings')
    (runtime / 'app').mkdir(parents=True)
    (runtime / 'app' / 'sender').write_text('old binary')
    (runtime / 'venv').mkdir()
    (runtime / 'venv' / 'old').write_text('old runtime')
    units.mkdir()
    unit = units / 'hwmonitor-mqtt-pve.service'
    unit.write_text('Description=Hardware Monitor MQTT sender for Proxmox VE\nExecStart=' + str(runtime / 'app' / 'sender') + '\n')
    override = units / 'hwmonitor-mqtt-pve.service.d' / '90-hwmonitor-sender.conf'
    override.parent.mkdir()
    override.write_text('[Service]\nExecStart=\nExecStart=' + str(runtime / 'app' / 'sender') +
                        '\nWorkingDirectory=' + str(runtime / 'app') + '\nEnvironment=PYTHONIOENCODING=utf-8\n')
    return root, runtime, units, unit, override


def fake_systemd(runtime, units, calls, *, install_fails=False):
    def run(args, **kwargs):
        calls.append(args)
        if args[:2] == ['systemctl', 'is-active'] or args[:2] == ['systemctl', 'is-enabled']:
            return subprocess.CompletedProcess(args, 0, '', '')
        if args[:2] == ['systemctl', 'show']:
            text = ('path=' + str(runtime / 'venv' / 'bin' / 'python') if '--property=ExecStart' in args else '42')
            return subprocess.CompletedProcess(args, 0, text + '\n', '')
        if '-c' in args and any('hwmonitor_mqtt.platforms.installers.pve' in arg for arg in args):
            (runtime / 'app' / 'hwmonitor_mqtt').mkdir(parents=True)
            (runtime / 'app' / 'hwmonitor_mqtt' / 'sender.py').write_text('new source')
            (runtime / 'venv').mkdir()
            (units / 'hwmonitor-mqtt-pve.service').write_text('new source unit')
            return subprocess.CompletedProcess(args, 1 if install_fails else 0, '', '')
        return subprocess.CompletedProcess(args, 0, '', '')
    return run


def test_compiled_install_migrates_to_python_and_clears_known_override(existing_install):
    root, runtime, units, unit, override = existing_install
    calls = []
    activation = PveActivation(root, '/tmp/pve.env', runtime=runtime, units=units,
                               run=fake_systemd(runtime, units, calls), sleep=lambda _: None)
    activation.prepare()
    activation.activate()
    activation.finish()
    assert not override.exists()
    assert (runtime / 'app' / 'hwmonitor_mqtt' / 'sender.py').read_text() == 'new source'
    assert (root / '.env').read_text() == 'private settings'
    assert any(args[:2] == ['systemctl', 'stop'] for args in calls)


def test_failed_migration_restores_previous_service_and_files(existing_install):
    root, runtime, units, unit, override = existing_install
    old_unit, old_override = unit.read_text(), override.read_text()
    calls = []
    activation = PveActivation(root, '/tmp/pve.env', runtime=runtime, units=units,
                               run=fake_systemd(runtime, units, calls, install_fails=True), sleep=lambda _: None)
    activation.prepare()
    with pytest.raises(RuntimeError, match='startup'):
        activation.activate()
    activation.recover()
    assert (runtime / 'app' / 'sender').read_text() == 'old binary'
    assert (runtime / 'venv' / 'old').read_text() == 'old runtime'
    assert unit.read_text() == old_unit
    assert override.read_text() == old_override
    assert (root / '.env').read_text() == 'private settings'
    assert any(args[:2] == ['systemctl', 'restart'] for args in calls)


def test_unknown_override_is_never_replaced(existing_install):
    root, runtime, units, unit, override = existing_install
    override.write_text('[Service]\nUser=custom\n')
    calls = []
    activation = PveActivation(root, '/tmp/pve.env', runtime=runtime, units=units,
                               run=fake_systemd(runtime, units, calls), sleep=lambda _: None)
    with pytest.raises(RuntimeError, match='unrecognized'):
        activation.prepare()
    assert override.read_text() == '[Service]\nUser=custom\n'
    assert not any(args[:2] == ['systemctl', 'stop'] for args in calls)


def test_interrupted_backup_keeps_runtime_that_was_not_moved(existing_install, monkeypatch):
    from scripts import source_pve
    root, runtime, units, unit, override = existing_install
    original_replace = source_pve.os.replace
    def replace(old, new):
        if Path(old) == runtime / 'venv':
            raise OSError('simulated backup failure')
        original_replace(old, new)
    monkeypatch.setattr(source_pve.os, 'replace', replace)
    calls = []
    activation = PveActivation(root, '/tmp/pve.env', runtime=runtime, units=units,
                               run=fake_systemd(runtime, units, calls), sleep=lambda _: None)
    activation.prepare()
    with pytest.raises(OSError, match='backup failure'):
        activation.activate()
    activation.recover()
    assert (runtime / 'app' / 'sender').read_text() == 'old binary'
    assert (runtime / 'venv' / 'old').read_text() == 'old runtime'


def test_failed_metadata_backup_does_not_delete_existing_override(existing_install, monkeypatch):
    from scripts import source_pve
    root, runtime, units, unit, override = existing_install
    original_copy = source_pve.shutil.copy2
    old_override = override.read_text()
    def copy(old, new, *args, **kwargs):
        if Path(old) == override:
            raise OSError('simulated metadata failure')
        return original_copy(old, new, *args, **kwargs)
    monkeypatch.setattr(source_pve.shutil, 'copy2', copy)
    activation = PveActivation(root, '/tmp/pve.env', runtime=runtime, units=units,
                               run=fake_systemd(runtime, units, []), sleep=lambda _: None)
    activation.prepare()
    with pytest.raises(OSError, match='metadata failure'):
        activation.activate()
    activation.recover()
    assert override.read_text() == old_override
    assert (runtime / 'app' / 'sender').read_text() == 'old binary'


def test_refuses_existing_runtime_without_recognized_service(existing_install):
    root, runtime, units, unit, override = existing_install
    unit.unlink()
    override.unlink()
    activation = PveActivation(root, '/tmp/pve.env', runtime=runtime, units=units,
                               run=fake_systemd(runtime, units, []), sleep=lambda _: None)
    with pytest.raises(RuntimeError, match='unrecognized'):
        activation.prepare()
    assert (runtime / 'app' / 'sender').read_text() == 'old binary'


def test_unconfirmed_previous_stop_does_not_claim_recovery(existing_install):
    root, runtime, units, unit, override = existing_install
    base = fake_systemd(runtime, units, [])
    def run(args, **kwargs):
        if args[:2] == ['systemctl', 'stop']:
            return subprocess.CompletedProcess(args, 1, '', 'unknown stop result')
        return base(args, **kwargs)
    activation = PveActivation(root, '/tmp/pve.env', runtime=runtime, units=units,
                               run=run, sleep=lambda _: None)
    activation.prepare()
    with pytest.raises(RuntimeError, match='cannot stop'):
        activation.activate()
    with pytest.raises(RuntimeError, match='unconfirmed'):
        activation.recover()
    assert (runtime / 'app' / 'sender').read_text() == 'old binary'


def test_first_install_failure_before_unit_creation_is_recoverable(tmp_path):
    root, runtime, units = tmp_path / 'root', tmp_path / 'runtime', tmp_path / 'units'
    root.mkdir()
    units.mkdir()
    calls = []
    def run(args, **kwargs):
        calls.append(args)
        if args[:2] in (['systemctl', 'is-active'], ['systemctl', 'is-enabled']):
            return subprocess.CompletedProcess(args, 4, '', '')
        if args[:2] in (['systemctl', 'stop'], ['systemctl', 'disable']):
            return subprocess.CompletedProcess(args, 5, '', 'unit not found')
        if '-c' in args and any('hwmonitor_mqtt.platforms.installers.pve' in arg for arg in args):
            (runtime / 'app').mkdir(parents=True)
            (runtime / 'venv').mkdir()
            return subprocess.CompletedProcess(args, 1, '', 'venv setup failed')
        return subprocess.CompletedProcess(args, 0, '', '')
    activation = PveActivation(root, '/tmp/pve.env', runtime=runtime, units=units, run=run, sleep=lambda _: None)
    activation.prepare()
    with pytest.raises(RuntimeError, match='startup') as failure:
        activation.activate()
    activation.recover()
    assert not (runtime / 'app').exists()
    assert not (runtime / 'venv').exists()
    assert not list(runtime.glob('.source-pve-rollback-*'))
    logs = list(runtime.glob('.source-pve-failure-*.log'))
    assert len(logs) == 1
    assert logs[0].stat().st_mode & 0o777 == 0o600
    assert 'venv setup failed' in logs[0].read_text()
    assert str(logs[0]) in str(failure.value)
    PveActivation(root, '/tmp/pve.env', runtime=runtime, units=units, run=run).prepare()


def test_first_install_failure_after_unit_creation_disables_before_removal(tmp_path):
    root, runtime, units = tmp_path / 'root', tmp_path / 'runtime', tmp_path / 'units'
    root.mkdir()
    units.mkdir()
    unit = units / 'hwmonitor-mqtt-pve.service'
    calls = []
    def run(args, **kwargs):
        calls.append(args)
        if args[:2] in (['systemctl', 'is-active'], ['systemctl', 'is-enabled']):
            return subprocess.CompletedProcess(args, 4, '', '')
        if args[:2] in (['systemctl', 'stop'], ['systemctl', 'disable']):
            return subprocess.CompletedProcess(args, 0 if unit.exists() else 5, '', '')
        if '-c' in args and any('hwmonitor_mqtt.platforms.installers.pve' in arg for arg in args):
            (runtime / 'app').mkdir(parents=True)
            (runtime / 'venv').mkdir()
            unit.write_text('new unit')
            return subprocess.CompletedProcess(args, 1, '', 'startup failed')
        return subprocess.CompletedProcess(args, 0, '', '')
    activation = PveActivation(root, '/tmp/pve.env', runtime=runtime, units=units, run=run, sleep=lambda _: None)
    activation.prepare()
    with pytest.raises(RuntimeError, match='startup'):
        activation.activate()
    activation.recover()
    assert not unit.exists()
    assert sum(args[:2] == ['systemctl', 'disable'] for args in calls) == 1
    assert ['systemctl', 'stop', 'hwmonitor-mqtt-pve.service'] in calls


def test_adapter_units_and_snapshot_restored_on_install_failure(existing_install, tmp_path, monkeypatch):
    monkeypatch.setattr("scripts.source_pve._check_snapshot_path", lambda path: None)
    from hwmonitor_mqtt.platforms.installers.pve import SNAPSHOT_SERVICE, SNAPSHOT_TIMER

    root, runtime, units, unit, override = existing_install
    snapshot = tmp_path / 'run' / 'snapshot.json'
    snapshot.parent.mkdir()
    snapshot.write_text('old snapshot')
    for name in (SNAPSHOT_SERVICE, SNAPSHOT_TIMER):
        (units / name).write_text('old ' + name)
    calls = []
    base = fake_systemd(runtime, units, calls, install_fails=True)
    def run(args, **kwargs):
        if '-c' in args and any('hwmonitor_mqtt.platforms.installers.pve' in arg for arg in args):
            for name in (SNAPSHOT_SERVICE, SNAPSHOT_TIMER):
                (units / name).write_text('new')
            snapshot.write_text('new snapshot')
        if args[:2] == ['systemctl', 'is-enabled'] and args[-1] == SNAPSHOT_SERVICE:
            calls.append(args)
            return subprocess.CompletedProcess(args, 0, 'static\n', '')
        return base(args, **kwargs)
    activation = PveActivation(root, '/tmp/pve.env', runtime=runtime, units=units,
                               snapshot=snapshot, run=run, sleep=lambda _: None)
    activation.prepare()
    with pytest.raises(RuntimeError, match='startup'):
        activation.activate()
    activation.recover()
    assert snapshot.read_text() == 'old snapshot'
    for name in (SNAPSHOT_SERVICE, SNAPSHOT_TIMER):
        assert (units / name).read_text() == 'old ' + name
        assert ['systemctl', 'start', name] in calls
    assert ['systemctl', 'enable', SNAPSHOT_TIMER] in calls
    assert ['systemctl', 'enable', SNAPSHOT_SERVICE] not in calls
    assert calls.index(['systemctl', 'stop', SNAPSHOT_TIMER]) < calls.index(['systemctl', 'stop', SNAPSHOT_SERVICE])
    assert calls.index(['systemctl', 'start', SNAPSHOT_TIMER]) < calls.index(['systemctl', 'restart', 'hwmonitor-mqtt-pve.service'])


def test_new_adapter_removed_on_install_failure(existing_install, tmp_path, monkeypatch):
    monkeypatch.setattr("scripts.source_pve._check_snapshot_path", lambda path: None)
    from hwmonitor_mqtt.platforms.installers.pve import SNAPSHOT_SERVICE, SNAPSHOT_TIMER

    root, runtime, units, _, _ = existing_install
    snapshot = tmp_path / 'run' / 'snapshot.json'
    snapshot.parent.mkdir()
    calls = []
    base = fake_systemd(runtime, units, calls, install_fails=True)
    def run(args, **kwargs):
        if '-c' in args and any('hwmonitor_mqtt.platforms.installers.pve' in arg for arg in args):
            for name in (SNAPSHOT_SERVICE, SNAPSHOT_TIMER):
                (units / name).write_text('new')
            snapshot.write_text('new snapshot')
        return base(args, **kwargs)
    activation = PveActivation(root, '/tmp/pve.env', runtime=runtime, units=units,
                               snapshot=snapshot, run=run, sleep=lambda _: None)
    activation.prepare()
    with pytest.raises(RuntimeError, match='startup'):
        activation.activate()
    activation.recover()
    assert not snapshot.exists()
    for name in (SNAPSHOT_SERVICE, SNAPSHOT_TIMER):
        assert not (units / name).exists()
        assert ['systemctl', 'disable', name] in calls


def test_installer_python_uses_isolated_environment(existing_install, monkeypatch):
    root, runtime, units, _, _ = existing_install
    calls = []
    real_run = fake_systemd(runtime, units, calls)
    seen = []
    def run(args, **kwargs):
        if '-c' in args:
            seen.append((args, kwargs['env']))
        return real_run(args, **kwargs)
    monkeypatch.setenv('PYTHONPATH', '/attacker')
    monkeypatch.setenv('PYTHONHOME', '/attacker')
    activation = PveActivation(root, '/tmp/pve.env', runtime=runtime, units=units, run=run, sleep=lambda _: None)
    activation.prepare()
    activation.activate()
    activation.finish()
    assert seen[0][0][1] == '-I'
    assert 'PYTHONPATH' not in seen[0][1]
    assert 'PYTHONHOME' not in seen[0][1]
