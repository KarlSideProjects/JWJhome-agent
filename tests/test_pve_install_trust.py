import os
import pytest

from hwmonitor_mqtt.platforms.installers import pve


def test_untrusted_owned_directory_is_rejected(tmp_path):
    with pytest.raises(RuntimeError, match='untrusted'):
        pve._validate_trusted_path(tmp_path)


def test_direct_install_refuses_existing_app_before_copy(tmp_path, monkeypatch):
    app = tmp_path / 'runtime' / 'app'
    app.mkdir(parents=True)
    victim = tmp_path / 'victim'
    victim.write_text('safe')
    (app / 'hwmonitor_mqtt').symlink_to(victim)
    sender = tmp_path / 'source' / 'hwmonitor_mqtt' / 'agents' / 'agent_sender_async.py'
    sender.parent.mkdir(parents=True)
    sender.write_text('new')
    monkeypatch.setattr(pve, 'APP_ROOT', str(app))
    monkeypatch.setattr(pve, 'SERVICE_VENV', str(app.parent / 'venv'))
    monkeypatch.setattr(pve, 'SYSTEMD_DIR', str(tmp_path / 'units'))
    monkeypatch.setattr(pve, '_validate_trusted_path', lambda path: None)
    monkeypatch.setattr(pve, '_validate_executable', lambda path: None)
    monkeypatch.setattr(pve, '_validate_trusted_tree', lambda path: None)
    with pytest.raises(RuntimeError, match='existing'):
        pve.install(sender.parents[2], tmp_path / 'pve.env', which=lambda x: '/bin/true', geteuid=lambda: 0)
    assert victim.read_text() == 'safe'


def test_source_prepare_rejects_untrusted_runtime(tmp_path):
    from scripts.source_pve import PveActivation
    runtime = tmp_path / 'runtime'
    runtime.mkdir(mode=0o775)
    activation = PveActivation(tmp_path, '/tmp/config', runtime=runtime, units=tmp_path / 'units', run=lambda *a, **kw: pytest.fail('must not invoke systemctl'))
    with pytest.raises(RuntimeError, match='untrusted'):
        activation.prepare()
