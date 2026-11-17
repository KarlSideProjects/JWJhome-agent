import json
import os
from unittest.mock import patch

import pytest

from hwmonitor_mqtt.clients.pve import PveError
from hwmonitor_mqtt.clients.pve_snapshot import LocalPveConfig, PveSnapshotClient


@pytest.fixture
def snapshot(tmp_path):
    path = tmp_path / 'snapshot.json'
    data = {'version': 1, 'node': 'node1', 'observed_at': 1000, 'resources': [],
            'disks': [{'devpath': '/dev/sda'}], 'smart': {'/dev/sda': {'health': 'ok'}}, 'errors': {}}
    path.write_text(json.dumps(data))
    path.chmod(0o640)
    return path, data


@pytest.fixture
def client(snapshot):
    path, _ = snapshot
    # Temporary directories can have untrusted ancestors; production never bypasses them.
    real_fstat = os.fstat
    def trusted_directories(fd):
        info = real_fstat(fd)
        values = list(info)
        if os.path.isdir(f'/proc/self/fd/{fd}'):
            values[0] &= ~0o022
        values[4] = 0
        return os.stat_result(values)
    with patch('os.fstat', side_effect=trusted_directories):
        yield PveSnapshotClient(LocalPveConfig('cluster', 'node1'), path, clock=lambda: 1000)


def test_snapshot_pins_smart_until_next_collection(client, snapshot):
    path, data = snapshot
    assert client.disks_list() == data['disks']
    data['smart']['/dev/sda']['health'] = 'bad'
    path.write_text(json.dumps(data))
    assert client.disk_smart('/dev/sda') == {'health': 'ok'}
    client.disks_list()
    assert client.disk_smart('/dev/sda') == {'health': 'bad'}
    with pytest.raises(PveError):
        client.disk_smart('/dev/sdb')


def test_failure_and_stale_snapshot(client, snapshot):
    path, data = snapshot
    data['errors']['smart'] = {'/dev/sda': 'collection_failed'}
    path.write_text(json.dumps(data))
    client.disks_list()
    with pytest.raises(PveError, match='collection_failed'):
        client.disk_smart('/dev/sda')
    data['observed_at'] = 1
    path.write_text(json.dumps(data))
    with pytest.raises(PveError, match='invalid_snapshot'):
        client.resources()
    assert client.observed_at is None


def test_rejects_writable_file(client, snapshot):
    path, _ = snapshot
    path.chmod(0o666)
    with pytest.raises(PveError, match='invalid_snapshot'):
        client.resources()


def test_local_collectors_repeat_refresh_stale_and_partial(client, snapshot):
    from hwmonitor_mqtt.collectors.pve import PveCollector
    from hwmonitor_mqtt.collectors.smart import SmartCollector

    path, data = snapshot
    data['resources'] = [
        {'node': 'node1', 'type': 'qemu', 'vmid': 101, 'status': 'running', 'netin': 100},
        {'node': 'node1', 'type': 'lxc', 'vmid': 102, 'status': 'stopped'},
    ]
    data['smart']['/dev/sda'] = {'health': 'FAILED'}
    path.write_text(json.dumps(data))
    guests = PveCollector(client)
    smart_client = PveSnapshotClient(client.config, path, clock=lambda: 1000)
    smart = SmartCollector(pve_client=smart_client)
    first = guests.poll_once()
    assert first['observed_at'] == 1000
    assert [g['type'] for g in first['guests']] == ['qemu', 'lxc']
    assert smart.poll_once()['status'] == 'critical'
    data['resources'][0]['netin'] = 200
    path.write_text(json.dumps(data))
    assert guests.poll_once() == first
    data['observed_at'] = 1010
    data['errors'] = {'smart': {'/dev/sda': 'collection_failed'}}
    client.clock = smart_client.clock = lambda: 1010
    path.write_text(json.dumps(data))
    refreshed = guests.poll_once()
    assert refreshed['guests'][0]['network_io']['total']['rate']['rx_bytes_per_s'] == 10
    failed = smart.poll_once()
    assert failed['partial'] and failed['errors']['/dev/sda'] == 'collection_failed'
    assert failed['status'] == 'critical' and failed['alerts']
    client.clock = smart_client.clock = lambda: 2000
    assert guests.poll_once()['status'] == 'error'
    assert guests.snapshot()['guests'] == refreshed['guests']
    stale = smart.poll_once()
    assert stale['status'] == 'unavailable' and stale['alerts']
    assert stale['observed_at'] == 1000


def test_sender_initializes_independent_local_clients(monkeypatch):
    from hwmonitor_mqtt.agents import agent_sender_async as sender
    from hwmonitor_mqtt.collectors.pve import PveCollector

    monkeypatch.setenv('PVE_LOCAL_SNAPSHOT', '/run/hwmonitor-mqtt-pve/snapshot.json')
    monkeypatch.setenv('PVE_NODE', 'node1')
    monkeypatch.setenv('PVE_CLUSTER_ID', 'cluster')
    monkeypatch.delenv('PVE_ENABLED', raising=False)
    monkeypatch.setattr(PveCollector, 'start', lambda self: None)
    monkeypatch.setattr(sender, 'pve_collector', None)
    monkeypatch.setattr(sender, 'smart_collector', sender.SmartCollector())
    collector = sender.initialize_pve_collector()
    assert isinstance(collector.client, PveSnapshotClient)
    assert isinstance(sender.smart_collector.pve_client, PveSnapshotClient)
    assert collector.client is not sender.smart_collector.pve_client
    assert collector.client.config.node == 'node1'


def test_partial_smart_never_refreshes_retained_healthy_disk_time(client, snapshot):
    from hwmonitor_mqtt.collectors.smart import SmartCollector

    path, data = snapshot
    data['smart']['/dev/sda'] = {'health': 'PASSED'}
    path.write_text(json.dumps(data))
    smart = SmartCollector(pve_client=client)
    assert smart.poll_once()['status'] == 'ok'
    for timestamp in (1100, 2000, 3000):
        client.clock = lambda: timestamp
        data['observed_at'] = timestamp
        data['errors'] = {'smart': {'/dev/sda': 'collection_failed'}}
        path.write_text(json.dumps(data))
        result = smart.poll_once()
        assert result['status'] == 'unavailable'
        assert result['observed_at'] == 1000
        assert result['partial'] and '/dev/sda' in result['errors']
    data['observed_at'] = 3010
    data['errors'] = {}
    client.clock = lambda: 3010
    path.write_text(json.dumps(data))
    result = smart.poll_once()
    assert result['status'] == 'ok' and result['observed_at'] == 3010
