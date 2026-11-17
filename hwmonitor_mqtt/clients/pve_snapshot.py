"""Read a root-owned local PVE snapshot without invoking commands."""

from dataclasses import dataclass
import json
import os
import re
import stat
import time

from hwmonitor_mqtt.clients.pve import PveError


@dataclass(frozen=True)
class LocalPveConfig:
    cluster_id: str
    node: str


class PveSnapshotClient:
    """Use one instance per collector; disk SMART reads use its latest snapshot."""

    def __init__(self, config, path, *, clock=time.time, max_age=120):
        if any(not isinstance(x, str) or not re.fullmatch(r"[A-Za-z0-9_.-]{1,128}", x)
               for x in (config.cluster_id, config.node)):
            raise PveError("invalid_config")
        self.config, self.path, self.clock, self.max_age = config, os.path.abspath(path), clock, max_age
        self.observed_at = None
        self.smart_errors = {}
        self.partial = False
        self._snapshot = None

    def _read(self):
        self._snapshot = None
        self.observed_at = None
        self.smart_errors = {}
        self.partial = False
        fd = None
        try:
            parts = self.path.split('/')[1:]
            fd = os.open('/', os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            for index, part in enumerate(parts):
                info = os.fstat(fd)
                if info.st_uid != 0 or info.st_mode & 0o022 or not stat.S_ISDIR(info.st_mode):
                    raise ValueError
                flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK
                if index < len(parts) - 1:
                    flags |= os.O_DIRECTORY
                new = os.open(part, flags, dir_fd=fd)
                os.close(fd)
                fd = new
            info = os.fstat(fd)
            if info.st_uid != 0 or info.st_mode & 0o022 or not stat.S_ISREG(info.st_mode) or info.st_size > 1048576:
                raise ValueError
            with os.fdopen(fd, 'rb') as source:
                fd = None
                raw = source.read(1048577)
            if len(raw) > 1048576:
                raise ValueError
            data = json.loads(raw, parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
            if not isinstance(data, dict) or type(data.get('version')) is not int or data['version'] != 1 or data.get('node') != self.config.node:
                raise ValueError
            observed = data.get('observed_at')
            now = self.clock()
            if type(observed) is not int or observed > now + 5 or observed < now - self.max_age:
                raise ValueError
            for name in ('resources', 'disks'):
                value = data.get(name)
                if value is not None and (not isinstance(value, list) or any(not isinstance(row, dict) for row in value)):
                    raise ValueError
            if not isinstance(data.get('smart'), dict) or any(not isinstance(k, str) or not isinstance(v, dict) for k, v in data['smart'].items()):
                raise ValueError
            errors = data.get('errors')
            if not isinstance(errors, dict) or any(errors.get(k) not in (None, 'collection_failed') for k in ('resources', 'disks')):
                raise ValueError
            smart_errors = errors.get('smart', {})
            if not isinstance(smart_errors, dict) or any(not isinstance(k, str) or v != 'collection_failed' for k, v in smart_errors.items()):
                raise ValueError
            for k in ('invalid_disks', 'disk_limit'):
                if k in errors and (type(errors[k]) is not int or errors[k] < 0):
                    raise ValueError
            self._snapshot, self.observed_at = data, observed
            self.smart_errors = smart_errors.copy()
            self.partial = any(key in errors for key in ("disks", "smart", "invalid_disks", "disk_limit"))
        except (OSError, ValueError, TypeError, RecursionError, OverflowError):
            raise PveError('invalid_snapshot') from None
        finally:
            if fd is not None:
                os.close(fd)

    def _collection(self, name):
        self._read()
        data = self._snapshot
        if data[name] is None or data['errors'].get(name):
            raise PveError('collection_failed')
        return data[name]

    def resources(self):
        return self._collection('resources')

    def disks_list(self):
        return self._collection('disks')

    def disk_smart(self, path):
        data = self._snapshot
        if not isinstance(path, str) or data is None or data['disks'] is None or not any(
                path == (row.get('devpath') or row.get('path') or row.get('dev')) for row in data['disks']):
            raise PveError('invalid_response')
        if path in self.smart_errors or path not in data['smart']:
            raise PveError('collection_failed')
        return data['smart'][path]
