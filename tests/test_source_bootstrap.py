import os
from pathlib import Path
import shutil
import subprocess
import sys
import tarfile

import pytest


BOOTSTRAP = Path(__file__).resolve().parents[1] / 'bootstrap-source.sh'


def run_bootstrap(tmp_path, root):
    bindir = tmp_path / 'bin'
    bindir.mkdir()
    for name, body in {
        'id': '#!/bin/sh\necho 0\n',
        'uname': '#!/bin/sh\necho x86_64\n',
        'stat': '#!/bin/sh\nif [ "$2" = "%u" ]; then echo 0; else exec /usr/bin/stat "$@"; fi\n',
        'curl': f'#!/bin/sh\ntouch "{tmp_path / "contacted"}"\nexit 1\n',
    }.items():
        tool = bindir / name
        tool.write_text(body)
        tool.chmod(0o755)
    return subprocess.run(
        ['bash', str(BOOTSTRAP)],
        env={**os.environ, 'PATH': f'{bindir}:{os.environ["PATH"]}',
             'HWMONITOR_BOOTSTRAP_ROOT': str(root)},
        text=True, capture_output=True,
    )


def test_unrecognized_directory_is_not_changed_or_downloaded(tmp_path):
    root = tmp_path / 'root'
    root.mkdir()
    (root / 'keep').write_text('user data')
    result = run_bootstrap(tmp_path, root)
    assert result.returncode != 0
    assert (root / 'keep').read_text() == 'user data'
    assert not (tmp_path / 'contacted').exists()


def test_symlinked_marker_is_rejected_before_download(tmp_path):
    root = tmp_path / 'root'
    root.mkdir()
    original = tmp_path / 'marker'
    original.write_text('original')
    (root / '.hwmonitor-mqtt-bootstrap').symlink_to(original)
    (root / 'sender').write_text('old sender')
    result = run_bootstrap(tmp_path, root)
    assert result.returncode != 0
    assert original.read_text() == 'original'
    assert not (tmp_path / 'contacted').exists()


def test_symlinked_settings_are_rejected_before_download(tmp_path):
    root = tmp_path / 'root'
    root.mkdir()
    (root / '.hwmonitor-mqtt-bootstrap').touch()
    (root / 'sender').write_text('old sender')
    settings = tmp_path / 'settings'
    settings.write_text('private')
    (root / '.env').symlink_to(settings)
    result = run_bootstrap(tmp_path, root)
    assert result.returncode != 0
    assert settings.read_text() == 'private'
    assert not (tmp_path / 'contacted').exists()


def test_download_failure_preserves_existing_settings_and_sender(tmp_path):
    root = tmp_path / 'root'
    root.mkdir()
    root.chmod(0o755)
    (root / '.hwmonitor-mqtt-bootstrap').touch()
    (root / 'sender').write_text('old sender')
    (root / '.env').write_text('private settings')
    result = run_bootstrap(tmp_path, root)
    assert result.returncode != 0
    assert (tmp_path / 'contacted').exists()
    assert (root / 'sender').read_text() == 'old sender'
    assert (root / '.env').read_text() == 'private settings'


def test_invalid_commit_stops_before_fetching_source(tmp_path):
    bindir = tmp_path / 'bin'
    bindir.mkdir()
    for name, body in {
        'id': '#!/bin/sh\necho 0\n',
        'uname': '#!/bin/sh\necho x86_64\n',
        'stat': '#!/bin/sh\nif [ "$2" = "%u" ]; then echo 0; else exec /usr/bin/stat "$@"; fi\n',
        'curl': '''#!/bin/sh
printf '%s\\n' "$*" >> "$CALLS"
while [ "$1" != "--output" ]; do shift; done
printf '{"sha":"../bad"}' > "$2"
''',
    }.items():
        tool = bindir / name
        tool.write_text(body)
        tool.chmod(0o755)
    calls = tmp_path / 'calls'
    result = subprocess.run(
        ['bash', str(BOOTSTRAP)], text=True, capture_output=True,
        env={**os.environ, 'PATH': f'{bindir}:{os.environ["PATH"]}',
             'HWMONITOR_BOOTSTRAP_ROOT': str(tmp_path / 'root'), 'CALLS': str(calls),
             'PYTHONOPTIMIZE': '1'},
    )
    assert result.returncode != 0
    assert len(calls.read_text().splitlines()) == 1
    assert not (tmp_path / 'root').exists()


@pytest.mark.parametrize('refs', ['', '0000' + 'a' * 40 + ' refs/heads/master\n',
                                  ('003f' + 'a' * 40 + ' refs/heads/master\n') * 2,
                                  '001e# service=git-upload-pack\n0000' + ('003f' + 'a' * 40 + ' refs/heads/master\n') * 2 + '0000',
                                  'not-a-git-advertisement\n003f' + 'a' * 40 + ' refs/heads/master\n',
                                  '001e# service=git-upload-pack\n0000003f' + 'a' * 40 + ' refs/heads/master\n0008xx'])
def test_invalid_fallback_refs_stop_before_source_download(tmp_path, refs):
    bindir = tmp_path / 'bin'
    bindir.mkdir()
    for name, body in {
        'id': '#!/bin/sh\necho 0\n',
        'uname': '#!/bin/sh\necho x86_64\n',
        'stat': '#!/bin/sh\nif [ "$2" = "%u" ]; then echo 0; else exec /usr/bin/stat "$@"; fi\n',
        'curl': '''#!/bin/sh
url=
while [ "$#" -gt 0 ]; do
  case "$1" in https://*) url=$1 ;; --output) shift; out=$1 ;; esac
  shift
done
case "$url" in
  */commits/master) exit 22 ;;
  *info/refs*) printf '%s' "$REFS" > "$out" ;;
  *) touch "$UNEXPECTED"; exit 1 ;;
esac
''',
    }.items():
        tool = bindir / name
        tool.write_text(body)
        tool.chmod(0o755)
    result = subprocess.run(['bash', str(BOOTSTRAP)], text=True, capture_output=True,
        env={**os.environ, 'PATH': f'{bindir}:{os.environ["PATH"]}',
             'HWMONITOR_BOOTSTRAP_ROOT': str(tmp_path / 'root'), 'REFS': refs,
             'UNEXPECTED': str(tmp_path / 'unexpected')})
    assert result.returncode != 0
    assert 'Invalid source commit' in result.stderr
    assert not (tmp_path / 'unexpected').exists()


@pytest.mark.parametrize('pinned', [False, True])
@pytest.mark.parametrize('repo_root', ['hwmonitor-mqtt-', 'JWJhome-agent-'])
@pytest.mark.parametrize('existing', [False, True])
@pytest.mark.parametrize('target', ['linux', 'pve'])
@pytest.mark.parametrize('api_limited', [False, True])
def test_linux_bootstrap_passes_verified_source_to_installer(tmp_path, existing, target, api_limited, repo_root, pinned):
    commit = 'a' * 40
    root = tmp_path / 'root'
    if existing:
        root.mkdir()
        root.chmod(0o755)
        (root / '.hwmonitor-mqtt-bootstrap').touch()
        (root / 'sender').write_text('old sender')
        (root / '.env').write_text('private settings')
        (root / '.env').chmod(0o600)
    source = tmp_path / (repo_root + commit)
    (source / 'scripts').mkdir(parents=True)
    (source / 'hwmonitor_mqtt' / 'platforms').mkdir(parents=True)
    (source / 'hwmonitor_mqtt' / 'platforms' / 'detect.py').write_text(
        'from types import SimpleNamespace\n'
        f'def detect_target(): return SimpleNamespace(system="{target}", distribution="debian")\n')
    (source / 'hwmonitor_mqtt' / 'platforms' / 'pve_bootstrap.py').write_text(
        'from pathlib import Path\nimport os, sys\nassert "--local" in sys.argv\nPath(os.environ["PVE_PREPARED"]).write_text("yes")\n')
    (source / 'docs' / 'configuration').mkdir(parents=True)
    (source / 'docs' / 'configuration' / 'pve.env.example').write_text('example')
    (source / 'scripts' / 'source_linux_install.py').write_text(
        'from pathlib import Path\nimport sys\n'
        'import os\n'
        'assert sys.argv[4] != "pve" or Path(os.environ["PVE_PREPARED"]).exists()\n'
        'Path(sys.argv[2], "activation.ok").write_text(sys.argv[3] + " " + sys.argv[4])\n'
        'Path(sys.argv[2], ".hwmonitor-mqtt-bootstrap").write_text(sys.argv[3])\n')
    archive = tmp_path / 'source.tar.gz'
    with tarfile.open(archive, 'w:gz') as tar:
        tar.add(source, arcname=source.name)
    bindir = tmp_path / 'bin'
    bindir.mkdir()
    for name, body in {
        'id': '#!/bin/sh\necho 0\n',
        'uname': '#!/bin/sh\necho x86_64\n',
        'stat': '#!/bin/sh\nif [ "$2" = "%u" ]; then echo 0; else exec /usr/bin/stat "$@"; fi\n',
        'docker': '#!/bin/sh\nif [ "$1 $2" = "compose version" ]; then exit 0; fi\necho "No such object: sys-agent" >&2\nexit 1\n',
        'curl': '''#!/bin/sh
url=
while [ "$#" -gt 0 ]; do
  case "$1" in https://*) url=$1 ;; --output) shift; out=$1 ;; esac
  shift
done
printf '%s\\n' "$url" >> "$CALLS"
case "$url" in
  */commits/master) if [ "$API_LIMITED" = 1 ]; then exit 22; fi; printf '{"sha":"%s"}' "$COMMIT" > "$out" ;;
  *info/refs*) printf '001e# service=git-upload-pack\n0000003f%s refs/heads/master\n0000' "$COMMIT" > "$out" ;;
  */scripts/source_archive.py) cp "$HELPER" "$out" ;;
  *.tar.gz) cp "$ARCHIVE" "$out" ;;
  *) exit 1 ;;
esac
''',
    }.items():
        tool = bindir / name
        tool.write_text(body)
        tool.chmod(0o755)
    calls = tmp_path / 'calls'
    attacker = tmp_path / 'attacker'
    attacker.mkdir()
    marker = tmp_path / 'injected'
    (attacker / 'sitecustomize.py').write_text(f'from pathlib import Path; Path({str(marker)!r}).write_text("executed")\n')
    user_site = attacker / 'lib' / f'python{sys.version_info.major}.{sys.version_info.minor}' / 'site-packages'
    user_site.mkdir(parents=True)
    user_marker = tmp_path / 'user-site-injected'
    (user_site / 'sitecustomize.py').write_text(f'from pathlib import Path; Path({str(user_marker)!r}).touch()\n')
    result = subprocess.run(
        ['bash', str(BOOTSTRAP)] + (['--commit', commit] if pinned else []), text=True, capture_output=True, cwd=attacker,
        env={**os.environ, 'PATH': f'{bindir}:{os.environ["PATH"]}',
             'HWMONITOR_BOOTSTRAP_ROOT': str(root), 'CALLS': str(calls),
             'PYTHONPATH': str(attacker), 'PYTHONHOME': str(attacker), 'PYTHONINSPECT': '1',
             'PYTHONUSERBASE': str(attacker),
             'COMMIT': commit, 'API_LIMITED': str(int(api_limited)), 'HELPER': str(BOOTSTRAP.parent / 'scripts/source_archive.py'),
             'ARCHIVE': str(archive), 'PVE_PREPARED': str(tmp_path / 'pve-prepared')},
    )
    assert not marker.exists()
    assert not user_marker.exists()
    assert result.returncode == 0, result.stderr
    assert (root / 'activation.ok').read_text() == commit + ' ' + target
    assert (tmp_path / 'pve-prepared').exists() == (target == 'pve')
    if existing:
        assert (root / '.env').read_text() == 'private settings'
    assert not (root / '.hwmonitor-mqtt-bootstrap-pending').exists()
    requests = calls.read_text().splitlines()
    assert len(requests) == (2 if pinned else 4 if api_limited else 3)
    assert all(commit in url for url in requests[-2:])


@pytest.mark.parametrize('pinned', [False, True])
def test_linux_bootstrap_activates_source_and_preserves_existing_settings(tmp_path, pinned):
    commit = 'b' * 40
    root = tmp_path / 'root'
    root.mkdir(mode=0o755)
    root.chmod(0o755)
    (root / '.hwmonitor-mqtt-bootstrap').write_text('old version')
    (root / 'sender').write_text('old binary')
    (root / '.env').write_text('BROKER_HOST=broker\n')
    (root / '.env').chmod(0o600)
    source = tmp_path / ('hwmonitor-mqtt-' + commit)
    scripts = source / 'scripts'
    scripts.mkdir(parents=True)
    for name in ('source_linux_install.py', 'source_deploy.py', 'source_docker.py', 'source_config.py'):
        shutil.copy2(BOOTSTRAP.parent / 'scripts' / name, scripts / name)
    platforms = source / 'hwmonitor_mqtt' / 'platforms'
    platforms.mkdir(parents=True)
    (platforms / 'detect.py').write_text(
        'from types import SimpleNamespace\n'
        'def detect_target(): return SimpleNamespace(system="linux", distribution="debian")\n')
    for name in ('Dockerfile.source', 'Dockerfile.source.dockerignore',
                 'docker-compose.linux.yml', 'docker-compose.source.yml'):
        shutil.copy2(BOOTSTRAP.parent / name, source / name)
    archive = tmp_path / 'source.tar.gz'
    with tarfile.open(archive, 'w:gz') as tar:
        tar.add(source, arcname=source.name)
    state = tmp_path / 'docker-state'
    bindir = tmp_path / 'bin'
    bindir.mkdir()
    tools = {
        'id': '#!/bin/sh\necho 0\n',
        'uname': '#!/bin/sh\necho x86_64\n',
        'stat': '#!/bin/sh\nif [ "$2" = "%u" ]; then echo 0; else exec /usr/bin/stat "$@"; fi\n',
        'systemctl': '#!/bin/sh\nprintf "LoadState=not-found\\nActiveState=inactive\\nUnitFileState=\\n"\n',
        'curl': '''#!/bin/sh
while [ "$#" -gt 0 ]; do
  case "$1" in https://*) url=$1 ;; --output) shift; out=$1 ;; esac
  shift
done
case "$url" in
  */commits/master) printf '{"sha":"%s"}' "$COMMIT" > "$out" ;;
  */scripts/source_archive.py) cp "$HELPER" "$out" ;;
  *.tar.gz) cp "$ARCHIVE" "$out" ;;
  *) exit 1 ;;
esac
''',
        'docker': '''#!/usr/bin/env python3
import json, os, sys
from pathlib import Path
args = sys.argv[1:]
root = Path(os.environ['HWMONITOR_BOOTSTRAP_ROOT'])
state = Path(os.environ['DOCKER_STATE'])
if args[:2] == ['compose', 'version']:
    sys.exit(0)
if args[0] == 'compose' and 'config' in args:
    print(json.dumps({'services': {'sys_agent': {'build': {'context': str(root), 'dockerfile': 'Dockerfile.source'}}}})); sys.exit(0)
if args[:2] == ['inspect', 'sys-agent']:
    if not state.exists():
        print('No such object: sys-agent', file=sys.stderr); sys.exit(1)
    print(state.read_text()); sys.exit(0)
if args[0] == 'compose' and 'up' in args:
    data = {'Id': 'new-id', 'Image': 'sha256:new', 'RestartCount': 0,
            'State': {'Status': 'running', 'Running': True, 'Pid': 123},
            'Config': {'Image': 'test:latest', 'Labels': {
                'com.docker.compose.project.working_dir': str(root),
                'com.docker.compose.service': 'sys_agent',
                'com.docker.compose.project.config_files': str(root / 'docker-compose.linux.yml') + ',' + str(root / 'docker-compose.source.yml')}}}
    state.write_text(json.dumps(data)); sys.exit(0)
sys.exit(1)
''',
    }
    for name, body in tools.items():
        tool = bindir / name
        tool.write_text(body)
        tool.chmod(0o755)
    command = ['bash', '-s', '--', '--commit', commit] if pinned else ['bash', str(BOOTSTRAP)]
    result = subprocess.run(
        command, input=BOOTSTRAP.read_text() if pinned else None, text=True, capture_output=True, timeout=20,
        env={**os.environ, 'PATH': f'{bindir}:{os.environ["PATH"]}',
             'HWMONITOR_BOOTSTRAP_ROOT': str(root), 'DOCKER_STATE': str(state),
             'COMMIT': commit, 'HELPER': str(BOOTSTRAP.parent / 'scripts/source_archive.py'),
             'ARCHIVE': str(archive)},
    )
    assert result.returncode == 0, result.stderr
    assert (root / '.hwmonitor-mqtt-bootstrap').read_text() == commit + '\n'
    assert (root / '.env').read_text() == 'BROKER_HOST=broker\n'
    assert (root / 'hwmonitor_mqtt' / 'platforms' / 'detect.py').is_file()
    assert state.is_file()


def test_sitecustomize_fixture_executes_without_isolation(tmp_path):
    marker = tmp_path / 'executed'
    (tmp_path / 'sitecustomize.py').write_text(f'from pathlib import Path; Path({str(marker)!r}).touch()\n')
    result = subprocess.run([sys.executable, '-c', 'pass'], cwd=tmp_path,
                            env={**os.environ, 'PYTHONPATH': str(tmp_path)}, capture_output=True)
    assert result.returncode == 0
    assert marker.exists()


def test_pinned_invalid_commit_rejected_before_download(tmp_path):
    bindir = tmp_path / 'bin'
    bindir.mkdir()
    for name, body in {'id': '#!/bin/sh\necho 0\n', 'uname': '#!/bin/sh\necho x86_64\n',
                       'stat': '#!/bin/sh\nif [ "$2" = "%u" ]; then echo 0; else exec /usr/bin/stat "$@"; fi\n',
                       'curl': f'#!/bin/sh\ntouch "{tmp_path / "contacted"}"\nexit 1\n'}.items():
        path = bindir / name
        path.write_text(body)
        path.chmod(0o755)
    result = subprocess.run(['bash', str(BOOTSTRAP), '--commit', 'not-a-commit'], capture_output=True, text=True,
        env={**os.environ, 'PATH': f'{bindir}:{os.environ["PATH"]}', 'HWMONITOR_BOOTSTRAP_ROOT': str(tmp_path / 'root')})
    assert result.returncode != 0
    assert 'Invalid source commit' in result.stderr
    assert not (tmp_path / 'contacted').exists()


PERMISSION_WRAPPER = '''import os, sys
from types import SimpleNamespace
original = os.fstat
def fstat(fd):
    result = original(fd)
    return SimpleNamespace(st_uid=0, st_mode=result.st_mode, st_dev=result.st_dev, st_ino=result.st_ino)
os.fstat = fstat
original_stat = os.stat
def checked_stat(path, *args, **kwargs):
    result = original_stat(path, *args, **kwargs)
    if kwargs.get('dir_fd') is not None:
        return SimpleNamespace(st_uid=0, st_mode=result.st_mode, st_dev=result.st_dev, st_ino=result.st_ino)
    return result
os.stat = checked_stat
exec(sys.argv.pop(1))
'''


@pytest.mark.parametrize(('answer', 'expected_mode'), [('y\n', 0o755), ('是\n', 0o755), ('要\n', 0o755), ('bad\nyes\n', 0o755), ('no\n', 0o775), ('否\n', 0o775), ('不要\n', 0o775), ('\n', 0o775), ('\x04', 0o775)])
def test_permission_confirmation_uses_controlling_terminal(tmp_path, answer, expected_mode):
    import pty
    import select

    root = tmp_path / 'root'
    root.mkdir(mode=0o775)
    root.chmod(0o775)
    (root / '.hwmonitor-mqtt-bootstrap').touch()
    (root / 'sender').touch()
    (root / '.hwmonitor-mqtt-bootstrap').chmod(0o644)
    (root / 'sender').chmod(0o644)
    settings = root / '.env'
    settings.write_text('keep')
    settings.chmod(0o600)
    script = BOOTSTRAP.read_text().split("<<'PY_CONFIRM' || exit 1\n", 1)[1].split('\nPY_CONFIRM', 1)[0]
    wrapper = PERMISSION_WRAPPER
    pid, terminal = pty.fork()
    if pid == 0:
        os.execv(sys.executable, [sys.executable, '-I', '-c', wrapper, script, str(root)])
    output = b''
    try:
        while b'Enter = no]' not in output:
            assert select.select([terminal], [], [], 5)[0], output
            try:
                output += os.read(terminal, 4096)
            except OSError:
                pytest.fail(repr(output))
        os.write(terminal, answer.encode())
        _, status = os.waitpid(pid, 0)
    finally:
        os.close(terminal)
    assert os.waitstatus_to_exitcode(status) == (0 if expected_mode == 0o755 else 1)
    assert root.stat().st_mode & 0o777 == expected_mode
    assert settings.read_text() == 'keep'
    assert settings.stat().st_mode & 0o777 == 0o600
    assert b'0775 -> 0755' in output


def test_permission_confirmation_without_terminal_rejects(tmp_path):
    root = tmp_path / 'root'
    root.mkdir()
    root.chmod(0o775)
    (root / '.hwmonitor-mqtt-bootstrap').touch()
    (root / 'sender').touch()
    (root / '.hwmonitor-mqtt-bootstrap').chmod(0o644)
    (root / 'sender').chmod(0o644)
    script = BOOTSTRAP.read_text().split("<<'PY_CONFIRM' || exit 1\n", 1)[1].split('\nPY_CONFIRM', 1)[0]
    wrapper = PERMISSION_WRAPPER
    result = subprocess.run([sys.executable, '-I', '-c', wrapper, script, str(root)],
                            capture_output=True, text=True, start_new_session=True)
    assert result.returncode != 0
    assert 'Cannot safely confirm' in result.stderr
    assert root.stat().st_mode & 0o777 == 0o775


@pytest.mark.parametrize('replacement', ['symlink', 'directory'])
def test_permission_confirmation_rejects_replaced_directory(tmp_path, replacement):
    import pty
    import select

    root = tmp_path / 'root'
    root.mkdir()
    root.chmod(0o775)
    for name in ('.hwmonitor-mqtt-bootstrap', 'sender'):
        (root / name).touch(mode=0o644)
        (root / name).chmod(0o644)
    script = BOOTSTRAP.read_text().split("<<'PY_CONFIRM' || exit 1\n", 1)[1].split('\nPY_CONFIRM', 1)[0]
    pid, terminal = pty.fork()
    if pid == 0:
        os.execv(sys.executable, [sys.executable, '-I', '-c', PERMISSION_WRAPPER, script, str(root)])
    output = b''
    try:
        while b'Enter = no]' not in output:
            assert select.select([terminal], [], [], 5)[0], output
            output += os.read(terminal, 4096)
        original = tmp_path / 'original'
        root.rename(original)
        target = tmp_path / 'target'
        target.mkdir(mode=0o777)
        target.chmod(0o777)
        if replacement == 'symlink':
            root.symlink_to(target)
        else:
            target.rename(root)
            target = root
        os.write(terminal, b'y\n')
        _, status = os.waitpid(pid, 0)
    finally:
        os.close(terminal)
    assert os.waitstatus_to_exitcode(status) != 0
    assert original.stat().st_mode & 0o777 == 0o775
    assert target.stat().st_mode & 0o777 == 0o777


@pytest.mark.parametrize('marker', ['pending', 'sender'])
@pytest.mark.parametrize('replacement', ['symlink', 'writable'])
def test_permission_confirmation_rejects_replaced_nested_directory(tmp_path, marker, replacement):
    import pty
    import select

    root = tmp_path / 'root'
    root.mkdir(mode=0o775)
    root.chmod(0o775)
    parent_mode = tmp_path.stat().st_mode & 0o777
    settings = root / '.env'
    settings.write_text('keep')
    settings.chmod(0o600)
    names = ('.hwmonitor-mqtt-bootstrap-pending',) if marker == 'pending' else ('.hwmonitor-mqtt-bootstrap', 'sender')
    for name in names:
        (root / name).touch()
        (root / name).chmod(0o644)
    package = root / 'hwmonitor_mqtt'
    platforms = package / 'platforms'
    platforms.mkdir(parents=True)
    package.chmod(0o755)
    platforms.chmod(0o755)
    script = BOOTSTRAP.read_text().split("<<'PY_CONFIRM' || exit 1\n", 1)[1].split('\nPY_CONFIRM', 1)[0]
    pid, terminal = pty.fork()
    if pid == 0:
        os.execv(sys.executable, [sys.executable, '-I', '-c', PERMISSION_WRAPPER, script, str(root)])
    output = b''
    try:
        while b'Enter = no]' not in output:
            assert select.select([terminal], [], [], 5)[0], output
            output += os.read(terminal, 4096)
        original = package / 'old-platforms'
        platforms.rename(original)
        target = tmp_path / 'target'
        target.mkdir()
        target.chmod(0o777)
        if replacement == 'symlink':
            platforms.symlink_to(target)
        else:
            target.rename(platforms)
            target = platforms
        os.write(terminal, b'y\n')
        _, status = os.waitpid(pid, 0)
    finally:
        os.close(terminal)
    assert os.waitstatus_to_exitcode(status) != 0
    assert root.stat().st_mode & 0o777 == 0o775
    assert original.stat().st_mode & 0o777 == 0o755
    assert target.stat().st_mode & 0o777 == 0o777
    assert tmp_path.stat().st_mode & 0o777 == parent_mode
    assert settings.read_text() == 'keep' and settings.stat().st_mode & 0o777 == 0o600


@pytest.mark.parametrize('layout', ['pending', 'compiled', 'source', 'legacy-source'])
def test_permission_confirmation_accepts_recognized_layout(tmp_path, layout):
    import pty
    import select

    root = tmp_path / 'root'
    root.mkdir()
    root.chmod(0o775)
    marker = '.hwmonitor-mqtt-bootstrap-pending' if layout == 'pending' else '.hwmonitor-mqtt-bootstrap'
    (root / marker).touch()
    (root / marker).chmod(0o644)
    if layout == 'compiled':
        (root / 'sender').touch()
        (root / 'sender').chmod(0o644)
    if layout in ('source', 'legacy-source'):
        package = root / 'hwmonitor_mqtt'
        platforms = package / 'platforms'
        platforms.mkdir(parents=True)
        package.chmod(0o755)
        platforms.chmod(0o755)
        (platforms / 'install.py').touch()
        (platforms / 'install.py').chmod(0o664 if layout == 'legacy-source' else 0o644)
        if layout == 'legacy-source':
            package.chmod(0o775)
            platforms.chmod(0o775)
            (root / '.env').write_text('keep')
            (root / '.env').chmod(0o600)
    script = BOOTSTRAP.read_text().split("<<'PY_CONFIRM' || exit 1\n", 1)[1].split('\nPY_CONFIRM', 1)[0]
    pid, terminal = pty.fork()
    if pid == 0:
        os.execv(sys.executable, [sys.executable, '-I', '-c', PERMISSION_WRAPPER, script, str(root)])
    output = b''
    try:
        while b'Enter = no]' not in output:
            assert select.select([terminal], [], [], 5)[0], output
            output += os.read(terminal, 4096)
        os.write(terminal, b'y\n')
        _, status = os.waitpid(pid, 0)
    finally:
        os.close(terminal)
    assert os.waitstatus_to_exitcode(status) == 0
    assert root.stat().st_mode & 0o777 == 0o755
    if layout == 'legacy-source':
        assert (root / '.env').read_text() == 'keep'
        assert (root / '.env').stat().st_mode & 0o777 == 0o600
        assert package.stat().st_mode & 0o777 == 0o775
        assert platforms.stat().st_mode & 0o777 == 0o775
        assert (platforms / 'install.py').stat().st_mode & 0o777 == 0o664
