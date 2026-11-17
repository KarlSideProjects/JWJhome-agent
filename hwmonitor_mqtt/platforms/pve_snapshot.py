"""Write the local PVE snapshot into the protected runtime directory."""

import argparse
import grp
import json
import os
import secrets
import stat

from hwmonitor_mqtt.collectors.pve_local import collect_snapshot


_MAX_BYTES = 1048576
_GROUP = "hwmonitor-mqtt-pve-readers"


def _validate_directory(fd, *, final=False, gid=None):
    info = os.fstat(fd)
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o022:
        raise OSError("untrusted snapshot directory")
    if final and (stat.S_IMODE(info.st_mode) != 0o750 or (gid is not None and info.st_gid != gid)):
        raise OSError("invalid snapshot directory permissions")


def _open_directory(gid):
    if gid <= 0:
        raise OSError("invalid snapshot reader group")
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
    fd = os.open('/', flags)
    try:
        _validate_directory(fd)
        for name in ('run', 'hwmonitor-mqtt-pve'):
            child = os.open(name, flags, dir_fd=fd)
            os.close(fd)
            fd = child
            _validate_directory(fd, final=name == 'hwmonitor-mqtt-pve', gid=gid)
        return fd
    except BaseException:
        os.close(fd)
        raise


def _write_snapshot(dir_fd, snapshot, gid):
    data = json.dumps(snapshot, allow_nan=False, separators=(',', ':')).encode('utf-8')
    if len(data) > _MAX_BYTES:
        raise ValueError('snapshot too large')
    try:
        existing = os.stat('snapshot.json', dir_fd=dir_fd, follow_symlinks=False)
    except FileNotFoundError:
        pass
    else:
        if not stat.S_ISREG(existing.st_mode):
            raise OSError('invalid snapshot target')
    name = '.snapshot-' + secrets.token_hex(16)
    fd = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
                 0o600, dir_fd=dir_fd)
    try:
        with os.fdopen(fd, 'wb') as stream:
            os.fchmod(stream.fileno(), 0o640)
            os.fchown(stream.fileno(), -1, gid)
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, 'snapshot.json', src_dir_fd=dir_fd, dst_dir_fd=dir_fd)
        os.fsync(dir_fd)
    finally:
        try:
            os.unlink(name, dir_fd=dir_fd)
        except FileNotFoundError:
            pass


def main():
    argparse.ArgumentParser(description=__doc__).parse_args()
    if os.geteuid() != 0:
        raise SystemExit('root required')
    if os.getegid() != 0:
        raise SystemExit('root primary group required')
    gid = grp.getgrnam(_GROUP).gr_gid
    fd = _open_directory(gid)
    try:
        _write_snapshot(fd, collect_snapshot(), gid)
    finally:
        os.close(fd)


if __name__ == '__main__':
    main()
