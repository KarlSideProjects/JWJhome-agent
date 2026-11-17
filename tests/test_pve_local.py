import json

from hwmonitor_mqtt.collectors.pve_local import collect_snapshot


def test_snapshot_filters_local_guests_and_keeps_partial_disk_results():
    calls = []

    def runner(args, timeout):
        calls.append(args)
        endpoint = args[3]
        if endpoint == '/cluster/resources':
            return json.dumps([{'node': 'host', 'vmid': 1}, {'node': 'other', 'vmid': 2}])
        if endpoint.endswith('/disks/list'):
            return json.dumps([{'devpath': '/dev/sda'}, {'devpath': '/dev/sdb'}, {'devpath': '/dev/../bad'}, {'devpath': '/dev/bad\nname'}])
        if args[-1] == '/dev/sdb':
            raise RuntimeError('failure')
        return json.dumps({'health': 'PASSED'})

    result = collect_snapshot(runner=runner, hostname=lambda: 'host', clock=lambda: 123)
    assert result['observed_at'] == 123
    assert result['resources'] == [{'node': 'host', 'vmid': 1}]
    assert result['smart'] == {'/dev/sda': {'health': 'PASSED'}}
    assert '/dev/sdb' in result['errors']['smart']
    assert result['errors']['invalid_disks'] == 2
    assert all(call[0] == '/usr/bin/pvesh' and call[1:3] == ['--noproxy', 'get'] for call in calls)
    assert all(call.count('--noproxy') == 1 for call in calls)
    assert len(calls) == 4


def test_source_failure_does_not_hide_other_sources():
    def runner(args, timeout):
        if args[3] == '/cluster/resources':
            raise RuntimeError('failure')
        return '[]'

    result = collect_snapshot(runner=runner, hostname=lambda: 'host', clock=lambda: 1)
    assert result['resources'] is None
    assert result['disks'] == []
    assert result['errors']['resources'] == 'collection_failed'


def test_invalid_hostname_is_rejected():
    import pytest
    with pytest.raises(ValueError):
        collect_snapshot(runner=lambda args, timeout: '[]', hostname=lambda: '../bad')


def test_successful_snapshot():
    def runner(args, timeout):
        if args[3] == '/cluster/resources':
            return '[{"node":"host","vmid":100}]'
        if args[3].endswith('/disks/list'):
            return '[{"devpath":"/dev/sda"}]'
        return '{"health":"PASSED"}'

    result = collect_snapshot(runner=runner, hostname=lambda: 'host', clock=lambda: 10)
    assert result == {'version': 1, 'node': 'host', 'observed_at': 10,
                      'resources': [{'node': 'host', 'vmid': 100}],
                      'disks': [{'devpath': '/dev/sda'}],
                      'smart': {'/dev/sda': {'health': 'PASSED'}}, 'errors': {}}


def test_fqdn_uses_proxmox_short_node():
    result = collect_snapshot(runner=lambda args, timeout: '[]', hostname=lambda: 'host.example', clock=lambda: 1)
    assert result['node'] == 'host'


def test_deadline_and_disk_limit_are_reported():
    now = [0]
    calls = []

    def runner(args, timeout):
        calls.append((args, timeout))
        if args[3] == '/cluster/resources':
            return '[]'
        if args[3].endswith('/disks/list'):
            return json.dumps([{'devpath': f'/dev/disk{i}'} for i in range(70)])
        now[0] = 31
        return '{}'

    result = collect_snapshot(runner=runner, hostname=lambda: 'host', clock=lambda: 1, monotonic=lambda: now[0])
    assert result['errors']['disk_limit'] == 6
    assert len(result['errors']['smart']) == 63
    assert len(calls) == 3
    assert all(timeout <= 15 for _, timeout in calls)


def test_oversize_response_is_a_source_failure():
    def runner(args, timeout):
        return ' ' * 1048577 if args[3] == '/cluster/resources' else '[]'

    result = collect_snapshot(runner=runner, hostname=lambda: 'host', clock=lambda: 1)
    assert result['errors']['resources'] == 'collection_failed'
    assert result['disks'] == []


def test_timeout_kills_descendant_without_touching_other_process(tmp_path):
    import os
    import subprocess
    import sys
    import time
    from unittest.mock import patch
    import pytest
    from hwmonitor_mqtt.collectors import pve_local

    pidfile = tmp_path / 'child.pid'
    code = ("import subprocess,time; child=subprocess.Popen(['sleep','60']); "
            f"open({str(pidfile)!r},'w').write(str(child.pid)); time.sleep(60)")
    actual_popen = subprocess.Popen
    unrelated = actual_popen(['sleep', '60'])
    def spawn(*args, **kwargs):
        return actual_popen([sys.executable, '-c', code], **kwargs)
    def trusted(path):
        return type('Info', (), {'st_uid': 0, 'st_mode': 0o100755 if path == pve_local._PVESH else 0o40755})()
    child_pid = None
    try:
        with patch.object(pve_local.os, 'lstat', side_effect=trusted), patch.object(pve_local.subprocess, 'Popen', side_effect=spawn):
            with pytest.raises(TimeoutError):
                pve_local._run(['ignored'], 1)
        child_pid = int(pidfile.read_text())
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            try:
                state = open(f'/proc/{child_pid}/stat').read().split(') ')[1][0]
            except (FileNotFoundError, ProcessLookupError):
                break
            if state == 'Z':
                break
            time.sleep(0.02)
        else:
            pytest.fail('descendant remains running')
        assert unrelated.poll() is None
    finally:
        if child_pid is None and pidfile.exists():
            child_pid = int(pidfile.read_text())
        if child_pid is not None:
            try:
                os.kill(child_pid, 9)
            except ProcessLookupError:
                pass
        unrelated.kill()
        unrelated.wait()


def test_fixed_pvesh_path_ignores_environment_path(monkeypatch):
    import pytest
    from hwmonitor_mqtt.collectors import pve_local

    # Proxmox installs SCRIPTS to BINDIR: https://raw.githubusercontent.com/proxmox/pve-manager/master/bin/Makefile
    # BINDIR is /usr/bin: https://raw.githubusercontent.com/proxmox/pve-manager/master/defines.mk
    checked = []
    calls = []
    def trusted(path):
        checked.append(path)
        mode = 0o100755 if path == '/usr/bin/pvesh' else 0o40755
        return type('Info', (), {'st_uid': 0, 'st_mode': mode})()
    def fail_launch(args, **kwargs):
        calls.append((args, kwargs))
        raise FileNotFoundError
    monkeypatch.setenv('PATH', '/tmp/untrusted')
    monkeypatch.setattr(pve_local.os, 'lstat', trusted)
    monkeypatch.setattr(pve_local.subprocess, 'Popen', fail_launch)
    with pytest.raises(FileNotFoundError):
        pve_local._run(['/usr/bin/pvesh', '--noproxy'], 1)
    assert checked == ['/', '/usr', '/usr/bin', '/usr/bin/pvesh']
    assert calls[0][0][0] == '/usr/bin/pvesh'
    assert calls[0][1]['env']['PATH'] == '/usr/sbin:/usr/bin:/sbin:/bin'
    assert '/tmp/untrusted' not in calls[0][1]['env']['PATH']


def test_untrusted_bin_directory_prevents_execution(monkeypatch):
    import pytest
    from hwmonitor_mqtt.collectors import pve_local

    def untrusted(path):
        mode = 0o40777 if path == '/usr/bin' else 0o40755
        return type('Info', (), {'st_uid': 0, 'st_mode': mode})()
    monkeypatch.setattr(pve_local.os, 'lstat', untrusted)
    monkeypatch.setattr(pve_local.subprocess, 'Popen', lambda *args, **kwargs: pytest.fail('untrusted executable launched'))
    with pytest.raises(OSError, match='untrusted'):
        pve_local._run(['/usr/bin/pvesh'], 1)
