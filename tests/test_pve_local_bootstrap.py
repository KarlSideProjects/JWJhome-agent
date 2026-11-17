from hwmonitor_mqtt.platforms.pve_bootstrap import prepare_local


def test_fresh_local_identity_needs_no_ca(tmp_path):
    path = tmp_path / 'pve.env'
    prepare_local(path, node_name='host', cluster_name='lab')
    assert path.read_text() == 'PVE_NODE=host\nPVE_CLUSTER_ID=lab\n'
    assert path.stat().st_mode & 0o777 == 0o600


def test_existing_identity_and_secrets_are_preserved(tmp_path):
    path = tmp_path / 'pve.env'
    original = 'PVE_NODE=old-node\nPVE_CLUSTER_ID=old-cluster\nPVE_TOKEN_SECRET=private\nPVE_CA_FILE=/old/ca\n'
    path.write_text(original)
    prepare_local(path, node_name='old-node', cluster_name='new-cluster')
    assert path.read_text() == original


def test_existing_foreign_node_is_rejected_without_modification(tmp_path):
    import pytest
    from hwmonitor_mqtt.platforms.pve_bootstrap import PveSetupError
    path = tmp_path / 'pve.env'
    original = 'PVE_NODE=remote\nPVE_CLUSTER_ID=stable\nPVE_TOKEN_SECRET=private\n'
    path.write_text(original)
    with pytest.raises(PveSetupError, match='PVE_NODE differs'):
        prepare_local(path, node_name='local', cluster_name='new')
    assert path.read_text() == original


def test_local_entry_skips_token_and_ca(tmp_path, monkeypatch):
    from hwmonitor_mqtt.platforms import pve_bootstrap
    def forbidden(*args, **kwargs):
        raise AssertionError('local setup invoked API provisioning')
    monkeypatch.setattr(pve_bootstrap, 'provision_token', forbidden)
    monkeypatch.setattr(pve_bootstrap, 'preflight', forbidden)
    monkeypatch.setattr(pve_bootstrap.socket, 'gethostname', lambda: 'host.example')
    monkeypatch.setattr(pve_bootstrap, '_cluster_name', lambda: None)
    path = tmp_path / 'pve.env'
    assert pve_bootstrap.main(['--local', '--config', str(path), '--template', str(tmp_path / 'missing')]) == 0
    assert path.read_text() == 'PVE_NODE=host\nPVE_CLUSTER_ID=host\n'


def test_native_readiness_rejects_foreign_node(tmp_path, monkeypatch):
    from hwmonitor_mqtt.platforms.installers import pve
    import socket
    monkeypatch.setattr(socket, 'gethostname', lambda: 'local.example')
    path = tmp_path / 'pve.env'
    path.write_text('PVE_NODE=remote\nPVE_CLUSTER_ID=cluster\n')
    assert 'PVE_NODE differs' in pve.local_readiness(path)
