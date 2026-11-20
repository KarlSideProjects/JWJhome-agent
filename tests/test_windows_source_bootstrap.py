import os
from pathlib import Path
import shutil
import subprocess
import zipfile

import pytest

from _pwsh import pwsh_executable, run_pwsh


ROOT = Path(__file__).resolve().parents[1]
BOOTSTRAP = ROOT / 'bootstrap-source.ps1'
COMMIT = 'a' * 40


def download_function():
    text = BOOTSTRAP.read_text()
    return text.split('# Begin source download', 1)[1].split('# End source download', 1)[0]


def handoff_function():
    text = BOOTSTRAP.read_text()
    return text.split('# Begin 64-bit handoff', 1)[1].split('# End 64-bit handoff', 1)[0]


@pytest.mark.parametrize(('api_limited', 'pinned'), [(False, False), (True, False), (False, True)])
def test_download_pins_commit_and_validates_archive_before_activation(tmp_path, api_limited, pinned):
    archive = tmp_path / 'source.zip'
    prefix = 'hwmonitor-mqtt-' + COMMIT + '/'
    with zipfile.ZipFile(archive, 'w') as content:
        for name in ('hwmonitor_mqtt/agents/agent_sender_windows.py',
                     'scripts/windows/install_windows_sender_task.ps1',
                     'scripts/windows/start_windows_sender.ps1',
                     'scripts/windows/activate_source.ps1', 'pyproject.toml', 'uv.lock', 'install.sh', '.python-version', 'README.md'):
            content.writestr(prefix + name, 'content')
    stage = tmp_path / 'work'
    stage.mkdir()
    shell = r'''
$global:urls = [Collections.Generic.List[string]]::new()
function Invoke-RestMethod { param($Uri, $Headers) if ($env:PINNED -eq '1') { throw 'unexpected API call' }; if ($env:API_LIMITED -eq '1') { throw 'HTTP 403' }; @{ sha = $env:COMMIT } }
function Invoke-WebRequest { param([switch]$UseBasicParsing, $Uri, $OutFile) if ($Uri.Contains('info/refs')) { if ($env:PINNED -eq '1') { throw 'unexpected refs call' }; [IO.File]::WriteAllText($OutFile, ("001e# service=git-upload-pack`n0000003f" + $env:COMMIT + " refs/heads/master`n0000")); return }; $global:urls.Add($Uri); if ($Uri.EndsWith('.zip')) { Copy-Item $env:ARCHIVE $OutFile } else { Copy-Item $env:VALIDATOR $OutFile } }
''' + download_function() + r'''
$pin = if ($env:PINNED -eq '1') { $env:COMMIT } else { '' }
$result = Get-VerifiedSource -WorkDirectory $env:WORK -PinnedCommit $pin
if ($result.Commit -ne $env:COMMIT -or -not (Test-Path (Join-Path $result.Path 'uv.lock'))) { throw 'source missing' }
if ($global:urls.Count -ne 2 -or @($global:urls | Where-Object { -not $_.Contains($env:COMMIT) }).Count -ne 0) { throw 'URLs not pinned' }
'''
    result = run_pwsh(shell, env={'WORK': str(stage), 'COMMIT': COMMIT, 'API_LIMITED': str(int(api_limited)), 'PINNED': str(int(pinned)), 'ARCHIVE': str(archive),
                                  'VALIDATOR': str(ROOT / 'scripts/windows/validate_source_archive.ps1')})
    assert result.returncode == 0, result.stderr


def test_invalid_commit_stops_before_downloading(tmp_path):
    shell = r'''
$global:downloads = 0
function Invoke-RestMethod { param($Uri, $Headers) @{ sha = '../bad' } }
function Invoke-WebRequest { param([switch]$UseBasicParsing, $Uri, $OutFile) $global:downloads++ }
''' + download_function() + r'''
try { Get-VerifiedSource -WorkDirectory $env:WORK | Out-Null; throw 'accepted invalid commit' }
catch { if ($global:downloads -ne 0 -or $_.Exception.Message -ne 'Invalid source commit.') { throw }; exit 0 }
'''
    result = run_pwsh(shell, env={'WORK': str(tmp_path)})
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize('refs', ['', '0000' + 'a' * 40 + ' refs/heads/master\n',
                                  ('003f' + 'a' * 40 + ' refs/heads/master\n') * 2,
                                  '001e# service=git-upload-pack\n0000' + ('003f' + 'a' * 40 + ' refs/heads/master\n') * 2 + '0000',
                                  'not-a-git-advertisement\n003f' + 'a' * 40 + ' refs/heads/master\n',
                                  '001e# service=git-upload-pack\n0000003f' + 'a' * 40 + ' refs/heads/master\n0008xx'])
def test_fallback_rejects_invalid_or_ambiguous_master_refs(tmp_path, refs):
    shell = r'''
function Invoke-RestMethod { param($Uri, $Headers) throw 'HTTP 403' }
function Invoke-WebRequest { param([switch]$UseBasicParsing, $Uri, $OutFile)
    if (-not $Uri.Contains('info/refs')) { throw 'unexpected download' }
    [IO.File]::WriteAllText($OutFile, $env:REFS)
}
''' + download_function() + r'''
try { Get-VerifiedSource -WorkDirectory $env:WORK | Out-Null; throw 'accepted invalid refs' }
catch { if ($_.Exception.Message -ne 'Invalid source commit.') { throw }; exit 0 }
'''
    result = run_pwsh(shell, env={'WORK': str(tmp_path), 'REFS': refs})
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize('commit', ['', 'BAD', 'A' * 40, 'a' * 39, 'a' * 40 + '\n'])
def test_invalid_explicit_commit_stops_before_side_effects(tmp_path, commit):
    shell = r'''
function Invoke-WebRequest { throw 'unexpected download' }
function Invoke-RestMethod { throw 'unexpected API call' }
try { & $env:BOOTSTRAP -Commit $env:BAD_COMMIT; throw 'accepted invalid commit' }
catch { if ($_.Exception.Message -ne 'Invalid source commit.') { throw }; exit 0 }
'''
    result = run_pwsh(shell, env={'BOOTSTRAP': str(BOOTSTRAP), 'BAD_COMMIT': commit, 'ProgramData': str(tmp_path)})
    assert result.returncode == 0, result.stderr
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize('pinned', ['', COMMIT])
def test_handoff_runs_native_powershell_and_forwards_optional_commit(tmp_path, pinned):
    work = tmp_path / 'relaunch'
    work.mkdir()
    marker = tmp_path / 'child-result.txt'
    child_script = (
        'param([string]$Commit = "")\n'
        '[IO.File]::WriteAllText($env:MARKER, "$($PSBoundParameters.ContainsKey(\'Commit\')):$Commit")\n'
    )
    shell = handoff_function() + r'''
Invoke-Native64BitBootstrap -ScriptText $env:CHILD_SCRIPT -WorkDirectory $env:WORK -PinnedCommit $env:PINNED -NativePowerShell $env:NATIVE_PS
'''
    native_ps = shutil.which(pwsh_executable()) or pwsh_executable()
    result = run_pwsh(shell, env={
        'CHILD_SCRIPT': child_script,
        'WORK': str(work),
        'PINNED': pinned,
        'NATIVE_PS': native_ps,
        'MARKER': str(marker),
    })
    assert result.returncode == 0, result.stderr
    expected = f'True:{COMMIT}' if pinned else 'False:'
    assert marker.read_text(encoding='utf-8').strip() == expected


def test_handoff_rejects_missing_native_powershell_and_propagates_nonzero_exit(tmp_path):
    work = tmp_path / 'relaunch'
    work.mkdir()
    missing = tmp_path / 'missing-powershell.exe'
    shell_missing = handoff_function() + r'''
try {
    Invoke-Native64BitBootstrap -ScriptText 'exit 0' -WorkDirectory $env:WORK -NativePowerShell $env:MISSING
    throw 'accepted missing powershell'
} catch {
    if ($_.Exception.Message -ne 'Only Windows x64 with 64-bit PowerShell is supported.') { throw }
    exit 0
}
'''
    result = run_pwsh(shell_missing, env={'WORK': str(work), 'MISSING': str(missing)})
    assert result.returncode == 0, result.stderr
    assert list(work.iterdir()) == []

    shell_fail = handoff_function() + r'''
try {
    Invoke-Native64BitBootstrap -ScriptText 'exit 7' -WorkDirectory $env:WORK -NativePowerShell $env:NATIVE_PS
    throw 'accepted failed child'
} catch {
    if ($_.Exception.Message -ne '64-bit PowerShell bootstrap failed with exit code 7.') { throw }
    exit 0
}
'''
    native_ps = shutil.which(pwsh_executable()) or pwsh_executable()
    result = run_pwsh(shell_fail, env={'WORK': str(work), 'NATIVE_PS': native_ps})
    assert result.returncode == 0, result.stderr


def test_handoff_switches_from_32bit_windows_powershell_to_64bit(tmp_path):
    wow64_ps = Path(r'C:\Windows\SysWOW64\WindowsPowerShell\v1.0\powershell.exe')
    if not wow64_ps.is_file():
        pytest.skip('32-bit Windows PowerShell (SysWOW64) is unavailable')
    work = tmp_path / 'relaunch'
    work.mkdir()
    marker = tmp_path / 'arch-result.txt'
    child_script = (
        'param([string]$Commit = "")\n'
        '[IO.File]::WriteAllText($env:MARKER, "$([IntPtr]::Size):$Commit")\n'
    )
    runner = tmp_path / 'run32.ps1'
    runner.write_text(
        "$ErrorActionPreference = 'Stop'\n"
        + handoff_function()
        + "\nif ([IntPtr]::Size -ne 4) { throw 'expected 32-bit PowerShell host' }\n"
        + "Invoke-Native64BitBootstrap -ScriptText $env:CHILD_SCRIPT -WorkDirectory $env:WORK -PinnedCommit $env:PINNED\n",
        encoding='utf-8',
    )
    env = {
        **os.environ,
        'CHILD_SCRIPT': child_script,
        'WORK': str(work),
        'PINNED': COMMIT,
        'MARKER': str(marker),
    }
    result = subprocess.run(
        [str(wow64_ps), '-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', str(runner)],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert marker.read_text(encoding='utf-8').strip() == f'8:{COMMIT}'


@pytest.mark.parametrize(('proc_arch', 'wow64_arch', 'expected_ok'), [
    ('AMD64', '', True),
    ('x86', 'AMD64', True),
    ('ARM64', '', False),
    ('x86', 'ARM64', False),
    ('x86', '', False),
])
def test_os_x64_check_uses_env_and_is64bit_without_runtimeinformation(proc_arch, wow64_arch, expected_ok):
    line = next(
        l for l in BOOTSTRAP.read_text(encoding='utf-8').splitlines()
        if '$osArch =' in l
    )
    check_line = next(
        l for l in BOOTSTRAP.read_text(encoding='utf-8').splitlines()
        if 'Only Windows x64 is supported.' in l
    )
    shell = f'''
$env:PROCESSOR_ARCHITECTURE = '{proc_arch}'
if ('{wow64_arch}') {{ $env:PROCESSOR_ARCHITEW6432 = '{wow64_arch}' }} else {{ Remove-Item Env:PROCESSOR_ARCHITEW6432 -ErrorAction SilentlyContinue }}
{line}
try {{
    {check_line}
    if (-not [Environment]::Is64BitOperatingSystem) {{ throw 'unexpected pass on 32-bit OS' }}
    exit 0
}} catch {{
    if ($_.Exception.Message -eq 'Only Windows x64 is supported.') {{ exit 2 }}
    throw
}}
'''
    result = run_pwsh(shell)
    if expected_ok:
        assert result.returncode == 0, result.stderr
    else:
        assert result.returncode == 2, result.stderr
