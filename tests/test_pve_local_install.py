from types import SimpleNamespace

from hwmonitor_mqtt.platforms.installers import pve


import pytest


@pytest.fixture(autouse=True)
def trusted_fixture_paths(monkeypatch):
    monkeypatch.setattr(pve, '_validate_trusted_path', lambda path: None)
    monkeypatch.setattr(pve, '_validate_executable', lambda path: None)
    monkeypatch.setattr(pve, '_validate_trusted_tree', lambda path: None)


def test_units_use_fixed_adapter_and_reader_group(tmp_path):
    sender = pve.unit_text(tmp_path)
    service = pve.snapshot_unit_text('/opt/hwmonitor-mqtt-pve/venv/bin/python')
    timer = pve.snapshot_timer_text()
    assert 'DynamicUser=yes' in sender
    assert 'SupplementaryGroups=hwmonitor-mqtt-pve-readers' in sender
    assert 'Environment=PVE_LOCAL_SNAPSHOT=/run/hwmonitor-mqtt-pve/snapshot.json' in sender
    assert 'User=root\nGroup=hwmonitor-mqtt-pve-readers\n' in service
    assert 'ExecStart=!/opt/hwmonitor-mqtt-pve/venv/bin/python ' in service
    assert 'RuntimeDirectoryMode=0750' in service
    assert 'RuntimeDirectoryPreserve=yes' in service
    assert ' -m hwmonitor_mqtt.platforms.pve_snapshot' in service
    assert 'OnUnitInactiveSec=' in timer


def test_activation_samples_before_sender_and_rolls_back_units_on_error(tmp_path, monkeypatch):
    monkeypatch.setattr(pve, 'SYSTEMD_DIR', str(tmp_path))
    unit = tmp_path / pve.SERVICE_NAME
    unit.write_text('old sender')
    calls = []

    def run(args, **kwargs):
        calls.append(args)
        if args[1:3] == ['start', pve.SNAPSHOT_SERVICE]:
            return SimpleNamespace(returncode=1)
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(pve, '_ensure_reader_group', lambda run: None)
    monkeypatch.setattr(pve, '_verify_snapshot', lambda config: None)
    assert pve._activate_local(tmp_path, '/usr/bin/python3', tmp_path / 'pve.env', '/bin/systemctl', run) != 0
    assert unit.read_text() == 'old sender'
    assert not (tmp_path / pve.SNAPSHOT_SERVICE).exists()
    assert not (tmp_path / pve.SNAPSHOT_TIMER).exists()
    assert not any(args[1:3] == ['restart', pve.SERVICE_NAME] for args in calls)


def test_activation_validates_snapshot_before_sender(tmp_path, monkeypatch):
    monkeypatch.setattr(pve, 'SYSTEMD_DIR', str(tmp_path))
    calls = []
    monkeypatch.setattr(pve, '_ensure_reader_group', lambda run: calls.append('group'))
    monkeypatch.setattr(pve, '_verify_snapshot', lambda config: calls.append('verify'))
    def run(args, **kwargs):
        calls.append(args[1:])
        return SimpleNamespace(returncode=0)
    assert pve._activate_local(tmp_path, '/usr/bin/python3', tmp_path / 'pve.env', '/bin/systemctl', run) == 0
    assert calls.index(['start', pve.SNAPSHOT_SERVICE]) < calls.index('verify') < calls.index(['restart', pve.SERVICE_NAME])


def test_native_install_prepares_identity_and_activates_without_ca(tmp_path, monkeypatch):
    source = tmp_path / 'source'
    sender = source / 'hwmonitor_mqtt' / 'agents' / 'agent_sender_async.py'
    sender.parent.mkdir(parents=True)
    sender.write_text('')
    monkeypatch.setattr(pve, 'APP_ROOT', str(tmp_path / 'runtime' / 'app'))
    monkeypatch.setattr(pve, 'SERVICE_VENV', str(tmp_path / 'runtime' / 'venv'))
    monkeypatch.setattr(pve, 'SYSTEMD_DIR', str(tmp_path / 'units'))
    monkeypatch.setattr(pve, '_normalize_tree_permissions', lambda path: None)
    monkeypatch.setattr(pve, '_ensure_reader_group', lambda run: None)
    monkeypatch.setattr(pve, '_verify_snapshot', lambda config: None)
    calls = []
    def run(args, **kwargs):
        calls.append(args)
        return SimpleNamespace(returncode=0)
    def which(name):
        return {'systemctl': '/bin/systemctl', 'python3': '/usr/bin/python3'}.get(name)
    config = tmp_path / 'private' / 'pve.env'
    assert pve.install(source, config, run=run, which=which, geteuid=lambda: 0) == 0
    assert 'PVE_NODE=' in config.read_text()
    assert 'PVE_CLUSTER_ID=' in config.read_text()
    assert any(args[1:3] == ['start', pve.SNAPSHOT_SERVICE] for args in calls)
    assert (tmp_path / 'units' / pve.SNAPSHOT_TIMER).is_file()


def test_snapshot_failure_reports_sanitized_phase_and_rolls_back(tmp_path, monkeypatch, capsys):
    from hwmonitor_mqtt.clients.pve import PveError
    monkeypatch.setattr(pve, 'SYSTEMD_DIR', str(tmp_path))
    old = tmp_path / pve.SERVICE_NAME
    old.write_text('old sender')
    monkeypatch.setattr(pve, '_ensure_reader_group', lambda run: None)
    def fail(config):
        raise PveError('collection_failed')
    monkeypatch.setattr(pve, '_verify_snapshot', fail)
    def run(args, **kwargs):
        return SimpleNamespace(returncode=0)
    assert pve._activate_local(tmp_path, '/usr/bin/python3', tmp_path / 'pve.env', '/bin/systemctl', run) != 0
    message = capsys.readouterr().err
    assert 'snapshot verification' in message
    assert 'collection_failed' in message
    assert old.read_text() == 'old sender'


def test_timer_start_failure_reports_stage_and_restores_units(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(pve, 'SYSTEMD_DIR', str(tmp_path))
    old = tmp_path / pve.SERVICE_NAME
    old.write_text('old sender')
    monkeypatch.setattr(pve, '_ensure_reader_group', lambda run: None)
    monkeypatch.setattr(pve, '_verify_snapshot', lambda config: None)
    def run(args, **kwargs):
        failed = args[1:] == ['enable', '--now', pve.SNAPSHOT_TIMER]
        return SimpleNamespace(returncode=1 if failed else 0)
    assert pve._activate_local(tmp_path, '/usr/bin/python3', tmp_path / 'pve.env', '/bin/systemctl', run) != 0
    assert 'snapshot timer: operation_failed' in capsys.readouterr().err
    assert old.read_text() == 'old sender'
    assert not (tmp_path / pve.SNAPSHOT_SERVICE).exists()
    assert not (tmp_path / pve.SNAPSHOT_TIMER).exists()
