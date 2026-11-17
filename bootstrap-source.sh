#!/usr/bin/env bash
set -euo pipefail

for python_var in ${!PYTHON@}; do
    unset "$python_var"
done
export PYTHONNOUSERSITE=1

pinned_commit=
if (( $# )); then
    [[ $# == 2 && $1 == --commit && $2 =~ ^[0-9a-f]{40}$ ]] || {
        echo 'Invalid source commit; expected --commit followed by 40 lowercase hexadecimal characters.' >&2
        exit 1
    }
    pinned_commit=$2
fi

# Begin prerequisites
ensure_source_prerequisites() {
    local target=$1 distro=$2 codename=$3 scratch=$4 status installed
    configure_docker_repository() {
        local repository architecture key=/etc/apt/keyrings/hwmonitor-docker.asc
        local sources=/etc/apt/sources.list.d/hwmonitor-docker.sources
        case "$distro" in ubuntu) repository=ubuntu ;; debian|raspbian) repository=debian ;; *) echo 'Unsupported Docker distribution.' >&2; return 1 ;; esac
        [[ $codename =~ ^[a-z0-9]+$ ]] || { echo 'Invalid distribution codename.' >&2; return 1; }
        architecture=$(dpkg --print-architecture) || return 1
        [[ $architecture == amd64 || $architecture == arm64 ]] || { echo 'Unsupported Docker architecture.' >&2; return 1; }
        [[ ! -e $key && ! -L $key && ! -e $sources && ! -L $sources ]] || {
            echo 'Existing Docker repository files need review before continuing.' >&2; return 1;
        }
        DEBIAN_FRONTEND=noninteractive apt-get install -y ca-certificates curl || return 1
        install -d -m 0755 /etc/apt/keyrings || return 1
        curl --fail --location --silent --show-error "https://download.docker.com/linux/$repository/gpg" --output "$scratch/docker.asc" || return 1
        [[ -s "$scratch/docker.asc" ]] || { echo 'Docker signing key is empty.' >&2; return 1; }
        install -m 0644 "$scratch/docker.asc" "$key" || return 1
        printf 'Types: deb\nURIs: https://download.docker.com/linux/%s\nSuites: %s\nComponents: stable\nArchitectures: %s\nSigned-By: %s\n' \
            "$repository" "$codename" "$architecture" "$key" | tee "$sources" >/dev/null || {
                rm -f -- "$key" "$sources"; return 1;
            }
        apt-get update || { rm -f -- "$key" "$sources"; return 1; }
    }
    if [[ $target == pve ]]; then
        status=$(dpkg-query -W -f='${db:Status-Status}' python3-venv 2>/dev/null || true)
        if [[ $status != installed ]]; then
            apt-get update || echo 'Warning: using existing APT metadata.' >&2
            DEBIAN_FRONTEND=noninteractive apt-get install -y python3-venv || return 1
        fi
        return 0
    fi
    [[ $target == linux ]] || { echo 'Unsupported prerequisite target.' >&2; return 1; }
    if command -v docker >/dev/null 2>&1 && docker compose version >/dev/null 2>&1; then return 0; fi
    if ! command -v docker >/dev/null 2>&1; then
        installed=$(dpkg-query -W -f='${db:Status-Status}\n' containerd.io containerd docker-ce docker-ce-cli moby-engine runc 2>/dev/null || true)
        while IFS= read -r status; do
            [[ $status != installed ]] || { echo 'Conflicting container runtime; resolve it before installing Docker.' >&2; return 1; }
        done <<<"$installed"
        apt-get update || echo 'Warning: using existing APT metadata.' >&2
        if apt-cache show docker.io >/dev/null 2>&1 && apt-cache show docker-compose-v2 >/dev/null 2>&1; then
            DEBIAN_FRONTEND=noninteractive apt-get install -y docker.io docker-compose-v2 || return 1
        elif apt-cache show docker-ce >/dev/null 2>&1 && apt-cache show docker-compose-plugin >/dev/null 2>&1; then
            DEBIAN_FRONTEND=noninteractive apt-get install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin || return 1
        else
            configure_docker_repository || return 1
            DEBIAN_FRONTEND=noninteractive apt-get install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin || return 1
        fi
    else
        apt-get update || echo 'Warning: using existing APT metadata.' >&2
        if apt-cache show docker-compose-v2 >/dev/null 2>&1; then
            DEBIAN_FRONTEND=noninteractive apt-get install -y docker-compose-v2 || return 1
        elif apt-cache show docker-compose-plugin >/dev/null 2>&1; then
            DEBIAN_FRONTEND=noninteractive apt-get install -y docker-compose-plugin || return 1
        else
            configure_docker_repository || return 1
            DEBIAN_FRONTEND=noninteractive apt-get install -y docker-compose-plugin || return 1
        fi
    fi
    command -v docker >/dev/null 2>&1 && docker compose version >/dev/null 2>&1 || {
        echo 'Docker Compose v2 is unavailable after installation.' >&2; return 1;
    }
}
# End prerequisites

root=${HWMONITOR_BOOTSTRAP_ROOT:-/opt/hwmonitor-mqtt}
if [[ $(id -u) -ne 0 ]]; then
    echo 'Run this installer as root.' >&2
    exit 1
fi
machine=$(uname -m)
case "$machine" in
    x86_64|aarch64) ;;
    *) echo 'Unsupported architecture.' >&2; exit 1 ;;
esac
[[ -r /etc/os-release ]] || { echo 'Cannot identify this operating system.' >&2; exit 1; }
. /etc/os-release
case "${ID:-}" in debian|ubuntu|raspbian) ;; *) echo 'Unsupported Linux distribution.' >&2; exit 1 ;; esac
codename=${UBUNTU_CODENAME:-${VERSION_CODENAME:-}}
if ! command -v python3 >/dev/null 2>&1; then
    apt-get update || echo 'Warning: using existing APT metadata.' >&2
    DEBIAN_FRONTEND=noninteractive apt-get install -y python3 python3-venv
fi
python3 -I -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)' || {
    echo 'Python 3.11 or newer is required.' >&2; exit 1;
}
[[ "$root" == /* ]] || { echo 'Installation path must be absolute.' >&2; exit 1; }
parent=$(dirname -- "$root")
[[ -d "$parent" && ! -L "$parent" && $(realpath -e -- "$parent") == "$parent" ]] || {
    echo 'Untrusted installation parent directory.' >&2; exit 1;
}
[[ $(stat -c %u "$parent") == 0 ]] || { echo 'Installation parent must be root-owned.' >&2; exit 1; }
parent_mode=$(stat -c %a "$parent")
(( (8#$parent_mode & 8#022) == 0 )) || { echo 'Installation parent must not be writable by others.' >&2; exit 1; }
if [[ -L "$root" || ( -e "$root" && ! -d "$root" ) ]]; then
    echo 'Untrusted installation directory.' >&2
    exit 1
fi
if [[ -e "$root" ]]; then
    marker="$root/.hwmonitor-mqtt-bootstrap"
    if [[ -L "$root/.env" || -L "$root/hwmonitor_mqtt" || -L "$root/hwmonitor_mqtt/platforms" ]]; then
        echo 'Untrusted installation files.' >&2
        exit 1
    fi
    pending="$root/.hwmonitor-mqtt-bootstrap-pending"
    if [[ -L "$marker" || -L "$pending" ]] ||
       [[ ! -f "$pending" && ( ! -f "$marker" ||
          ( ( -L "$root/sender" || ! -f "$root/sender" ) &&
            ( -L "$root/hwmonitor_mqtt/platforms/install.py" || ! -f "$root/hwmonitor_mqtt/platforms/install.py" ) ) ) ]]; then
        echo "Refusing to use existing unrecognized directory: $root" >&2
        exit 1
    fi
fi

tmp=$(mktemp -d)
trap 'rm -rf -- "$tmp"' EXIT
if [[ -n $pinned_commit ]]; then
    commit=$pinned_commit
elif curl --fail --location --silent --show-error \
    https://api.github.com/repos/KarlSideProjects/hwmonitor-mqtt/commits/master \
    --output "$tmp/commit.json"; then
    commit=$(python3 -I -c 'import json,re,sys; s=json.load(open(sys.argv[1])).get("sha", ""); sys.exit("Invalid source commit") if not isinstance(s, str) or not re.fullmatch("[0-9a-f]{40}", s) else print(s)' "$tmp/commit.json")
else
    curl --fail --location --silent --show-error --max-filesize 10485760 \
        'https://github.com/KarlSideProjects/hwmonitor-mqtt.git/info/refs?service=git-upload-pack' \
        --output "$tmp/refs"
    commit=$(python3 -I - "$tmp/refs" <<'PY'
import pathlib, re, sys
b = pathlib.Path(sys.argv[1]).read_bytes()
packets, pos = [], 0
while pos < len(b):
    header = b[pos:pos + 4]
    if not re.fullmatch(rb'[0-9a-f]{4}', header): sys.exit('Invalid source commit')
    size = int(header, 16)
    pos += 4
    if size == 0:
        packets.append(None)
    elif size < 4 or pos + size - 4 > len(b):
        sys.exit('Invalid source commit')
    else:
        packets.append(b[pos:pos + size - 4])
        pos += size - 4
if len(packets) < 4 or packets[0] != b'# service=git-upload-pack\n' or packets[1] is not None or packets[-1] is not None or None in packets[2:-1]:
    sys.exit('Invalid source commit')
matches = [m.group(1).decode() for p in packets[2:-1] if (m := re.fullmatch(rb'([0-9a-f]{40}) refs/heads/master(?:\x00[^\n]*)?\n', p))]
if len(matches) != 1: sys.exit('Invalid source commit')
print(matches[0])
PY
    )
fi
curl --fail --location --silent --show-error \
    "https://raw.githubusercontent.com/KarlSideProjects/hwmonitor-mqtt/$commit/scripts/source_archive.py" \
    --output "$tmp/source_archive.py"
curl --fail --location --silent --show-error \
    "https://github.com/KarlSideProjects/hwmonitor-mqtt/archive/$commit.tar.gz" \
    --output "$tmp/source.tar.gz"
source_dir=$(python3 -I "$tmp/source_archive.py" "$tmp/source.tar.gz" "$tmp/unpacked")
[[ ( "$source_dir" == "$tmp/unpacked/hwmonitor-mqtt-$commit" || "$source_dir" == "$tmp/unpacked/JWJhome-agent-$commit" ) && -f "$source_dir/scripts/source_linux_install.py" ]] || {
    echo 'Downloaded source identity is invalid.' >&2; exit 1;
}
target=$(cd "$source_dir" && python3 -I -c 'import sys; sys.path.insert(0, sys.argv[1]); from hwmonitor_mqtt.platforms.detect import detect_target; t=detect_target(); print(t.system if t.system == "pve" or (t.system == "linux" and t.distribution in ("debian", "ubuntu", "raspbian")) else "unsupported")' "$source_dir")
[[ $target == linux || $target == pve ]] || { echo 'Unsupported or unrecognized host.' >&2; exit 1; }
[[ $target != pve || $machine == x86_64 ]] || { echo 'PVE requires x64.' >&2; exit 1; }
if [[ ! -e "$root" ]]; then
    mkdir -m 0755 "$root"
    (set -o noclobber; : > "$root/.hwmonitor-mqtt-bootstrap-pending")
fi
[[ ! -L "$root" && -d "$root" && $(stat -c %u "$root") == 0 ]] || { echo 'Untrusted installation directory.' >&2; exit 1; }
root_mode=$(stat -c %a "$root")
if (( (8#$root_mode & 8#022) != 0 )); then
    python3 -I - "$root" <<'PY_CONFIRM' || exit 1
import os, stat, sys

path = sys.argv[1]
def fail(message):
    sys.exit(message)

try:
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    before = os.fstat(fd)
    if before.st_uid != 0:
        fail('Untrusted installation directory.')
    def regular(name, directory=fd, writable=False):
        try:
            info = os.stat(name, dir_fd=directory, follow_symlinks=False)
            return stat.S_ISREG(info.st_mode) and info.st_uid == 0 and not info.st_mode & (0o002 if writable else 0o022)
        except FileNotFoundError:
            return False
    def is_link(name):
        try:
            return stat.S_ISLNK(os.stat(name, dir_fd=fd, follow_symlinks=False).st_mode)
        except FileNotFoundError:
            return False
    source_identity = None
    def source_paths(writable):
        opened = []
        identity = []
        try:
            parent = fd
            for name in ('hwmonitor_mqtt', 'platforms'):
                try:
                    child = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent)
                except FileNotFoundError:
                    return True, False, tuple(identity)
                opened.append(child)
                info = os.fstat(child)
                if info.st_uid != 0 or info.st_mode & (0o002 if writable else 0o022):
                    return False, False, ()
                identity.append((info.st_dev, info.st_ino, info.st_mode))
                parent = child
            installer = regular('install.py', parent, writable)
            if installer:
                info = os.stat('install.py', dir_fd=parent, follow_symlinks=False)
                identity.append((info.st_dev, info.st_ino, info.st_mode))
            return True, installer, tuple(identity)
        except OSError:
            return False, False, ()
        finally:
            for child in reversed(opened):
                os.close(child)
    def recognized():
        global source_identity
        legacy = not any(os.path.lexists(os.path.join(path, name)) for name in
                         ('.hwmonitor-mqtt-bootstrap-pending', 'sender'))
        safe_source, source_installer, identity = source_paths(legacy)
        if source_identity is None:
            source_identity = identity
        return (safe_source and identity == source_identity and
                not any(is_link(name) for name in ('.env', 'hwmonitor_mqtt', '.hwmonitor-mqtt-bootstrap', '.hwmonitor-mqtt-bootstrap-pending'))
                and (regular('.hwmonitor-mqtt-bootstrap-pending') or
                     (regular('.hwmonitor-mqtt-bootstrap') and (regular('sender') or source_installer))))
    if not recognized():
        fail('Untrusted or unrecognized installation directory.')
    def unchanged():
        current = os.stat(path, follow_symlinks=False)
        opened = os.fstat(fd)
        return (stat.S_ISDIR(current.st_mode) and current.st_dev == before.st_dev and
                current.st_ino == before.st_ino and opened.st_uid == 0 and
                opened.st_mode == before.st_mode)
    if not unchanged():
        fail('Installation directory changed; refusing to modify it.')
    mode = stat.S_IMODE(before.st_mode)
    with open('/dev/tty', 'r', encoding='utf-8') as tty_in, open('/dev/tty', 'w', encoding='utf-8') as tty:
        tty.write(f'Installation directory: {path}\nPermissions: {mode:04o} -> {mode & ~0o022:04o}\n'
                  'Remove group and other write access? This restricts who can modify the installation directory.\n')
        while True:
            tty.write('Continue? [y/yes/是/要; n/no/否/不要; Enter = no] ')
            tty.flush()
            answer = tty_in.readline()
            if not answer or answer.strip().lower() in ('n', 'no', '否', '不要', ''):
                fail('Installation directory permissions unchanged.')
            if answer.strip().lower() in ('y', 'yes', '是', '要'):
                break
            tty.write('Please answer yes or no.\n')
    if not unchanged() or not recognized():
        fail('Installation directory changed; refusing to modify it.')
    os.fchmod(fd, mode & ~0o022)
    after = os.stat(path, follow_symlinks=False)
    if (not stat.S_ISDIR(after.st_mode) or (after.st_dev, after.st_ino) != (before.st_dev, before.st_ino)
            or stat.S_IMODE(os.fstat(fd).st_mode) != mode & ~0o022 or not recognized()):
        fail('Installation directory changed; refusing to continue.')
except OSError as exc:
    fail(f'Cannot safely confirm installation directory permissions: {exc}')
PY_CONFIRM
fi
lock="$root/.sender-install.lock"
[[ ! -L "$lock" && ( ! -e "$lock" || ( -f "$lock" && $(stat -c %u "$lock") == 0 ) ) ]] || {
    echo 'Untrusted installation lock.' >&2; exit 1;
}
exec 9>>"$lock"
flock -n 9 || { echo 'Another installation is running.' >&2; exit 1; }
ensure_source_prerequisites "$target" "$ID" "$codename" "$tmp"
if [[ $target == pve ]]; then
    if command -v docker >/dev/null 2>&1; then
        if docker inspect sys-agent >/dev/null 2>"$tmp/docker-error"; then
            echo 'Existing Docker sender must be migrated before installing native PVE.' >&2; exit 1
        elif ! grep -qi 'No such object' "$tmp/docker-error"; then
            echo 'Cannot verify existing Docker sender.' >&2; exit 1
        fi
    fi
    pve_config=${PVE_CONFIG_FILE:-/etc/hwmonitor-mqtt/pve.env}
    (cd "$source_dir" && python3 -I -c 'import runpy,sys; sys.path.insert(0,sys.argv.pop(1)); runpy.run_module("hwmonitor_mqtt.platforms.pve_bootstrap",run_name="__main__",alter_sys=True)' "$source_dir" \
        --config "$pve_config" --template "$source_dir/docs/configuration/pve.env.example" --local)
fi
(cd "$source_dir" && python3 -I -c 'import runpy,sys; sys.path.insert(0,sys.argv.pop(1)); runpy.run_module("scripts.source_linux_install",run_name="__main__",alter_sys=True)' "$source_dir" "$source_dir" "$root" "$commit" "$target")
rm -f -- "$root/.hwmonitor-mqtt-bootstrap-pending"
echo "Sender source $commit installed."
