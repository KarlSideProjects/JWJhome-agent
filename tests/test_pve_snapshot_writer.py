import json
import os
import stat

import pytest

from hwmonitor_mqtt.platforms import pve_snapshot


def test_write_atomically_and_permissions(tmp_path):
    fd = os.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        pve_snapshot._write_snapshot(fd, {'version': 1}, os.getgid())
    finally:
        os.close(fd)
    target = tmp_path / 'snapshot.json'
    assert json.loads(target.read_text()) == {'version': 1}
    assert stat.S_IMODE(target.stat().st_mode) == 0o640
    assert target.stat().st_gid == os.getgid()
    assert list(tmp_path.iterdir()) == [target]


def test_failed_replace_preserves_old_snapshot(tmp_path, monkeypatch):
    target = tmp_path / 'snapshot.json'
    target.write_text('old')
    def fail(*args, **kwargs):
        raise OSError('interrupted')
    monkeypatch.setattr(pve_snapshot.os, 'replace', fail)
    fd = os.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        with pytest.raises(OSError):
            pve_snapshot._write_snapshot(fd, {'version': 1}, os.getgid())
    finally:
        os.close(fd)
    assert target.read_text() == 'old'
    assert list(tmp_path.iterdir()) == [target]


def test_reject_symlink_and_oversize(tmp_path):
    target = tmp_path / 'snapshot.json'
    target.symlink_to('victim')
    fd = os.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        with pytest.raises(OSError):
            pve_snapshot._write_snapshot(fd, {}, os.getgid())
        target.unlink()
        with pytest.raises(ValueError):
            pve_snapshot._write_snapshot(fd, {'data': 'x' * 1048576}, os.getgid())
        with pytest.raises(ValueError):
            pve_snapshot._write_snapshot(fd, {'data': float('nan')}, os.getgid())
    finally:
        os.close(fd)
    assert list(tmp_path.iterdir()) == []


def test_directory_validation_rejects_nonroot_and_writable(tmp_path):
    fd = os.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        with pytest.raises(OSError):
            pve_snapshot._validate_directory(fd, final=True)
    finally:
        os.close(fd)


def test_open_directory_rejects_symlink_component(tmp_path, monkeypatch):
    (tmp_path / 'run').symlink_to(tmp_path, target_is_directory=True)
    real_open = os.open

    def redirected_open(path, flags, *args, **kwargs):
        return real_open(tmp_path if path == '/' else path, flags, *args, **kwargs)

    monkeypatch.setattr(pve_snapshot.os, 'open', redirected_open)
    monkeypatch.setattr(pve_snapshot, '_validate_directory', lambda *args, **kwargs: None)
    with pytest.raises(OSError):
        pve_snapshot._open_directory(10)


def test_directory_permission_checks(monkeypatch):
    from types import SimpleNamespace
    for owner, mode, group in [(1, 0o750, 10), (0, 0o770, 10), (0, 0o755, 10), (0, 0o750, 11), (0, 0o750, 0)]:
        monkeypatch.setattr(pve_snapshot.os, 'fstat', lambda fd: SimpleNamespace(
            st_uid=owner, st_gid=group, st_mode=stat.S_IFDIR | mode))
        with pytest.raises(OSError):
            pve_snapshot._validate_directory(42, final=True, gid=10)
    monkeypatch.setattr(pve_snapshot.os, 'fstat', lambda fd: SimpleNamespace(
        st_uid=0, st_gid=10, st_mode=stat.S_IFDIR | 0o750))
    pve_snapshot._validate_directory(42, final=True, gid=10)



def test_open_directory_requires_existing_reader_group(tmp_path, monkeypatch):
    from types import SimpleNamespace
    root = tmp_path / 'run' / 'hwmonitor-mqtt-pve'
    root.mkdir(parents=True, mode=0o750)
    root.parent.chmod(0o755)
    real_open, real_fstat = os.open, os.fstat
    final_inode = root.stat().st_ino
    group = [0]

    def redirected_open(path, flags, *args, **kwargs):
        return real_open(tmp_path if path == '/' else path, flags, *args, **kwargs)

    def trusted_stat(fd):
        info = real_fstat(fd)
        return SimpleNamespace(st_uid=0, st_gid=group[0] if info.st_ino == final_inode else 0,
                               st_mode=info.st_mode)

    monkeypatch.setattr(pve_snapshot.os, 'open', redirected_open)
    monkeypatch.setattr(pve_snapshot.os, 'fstat', trusted_stat)
    monkeypatch.setattr(pve_snapshot.os, 'fchown', lambda *args: pytest.fail('directory must not be changed'))
    with pytest.raises(OSError):
        pve_snapshot._open_directory(10)
    group[0] = 10
    fd = pve_snapshot._open_directory(10)
    os.close(fd)

def test_cli_rejects_non_root(monkeypatch):
    import sys
    monkeypatch.setattr(sys, 'argv', ['pve_snapshot'])
    monkeypatch.setattr(pve_snapshot.os, 'geteuid', lambda: 1000)
    with pytest.raises(SystemExit, match='root required'):
        pve_snapshot.main()


def test_cli_rejects_root_user_with_nonroot_primary_group(monkeypatch):
    import sys
    monkeypatch.setattr(sys, 'argv', ['pve_snapshot'])
    monkeypatch.setattr(pve_snapshot.os, 'geteuid', lambda: 0)
    monkeypatch.setattr(pve_snapshot.os, 'getegid', lambda: 1001)
    monkeypatch.setattr(pve_snapshot.grp, 'getgrnam', lambda name: pytest.fail('must reject before group lookup'))
    with pytest.raises(SystemExit, match='root'):
        pve_snapshot.main()


def test_root_group_cannot_be_used_as_reader_group(monkeypatch):
    monkeypatch.setattr(pve_snapshot.os, 'open', lambda *args, **kwargs: pytest.fail('must reject before opening'))
    with pytest.raises(OSError, match='reader group'):
        pve_snapshot._open_directory(0)
