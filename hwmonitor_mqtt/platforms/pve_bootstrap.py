"""Prepare and validate the host-local PVE sender configuration."""

import argparse
import os
import tempfile
from pathlib import Path
import re
import shutil
import socket
import subprocess
import sys

from hwmonitor_mqtt.clients.pve import PveApiClient, PveConfig, PveError
from hwmonitor_mqtt.platforms.pve_token import PveTokenError, provision_token


DEFAULT_CA = Path("/etc/hwmonitor-mqtt/pve-root-ca.pem")
LOCAL_CA = Path("/etc/pve/pve-root-ca.pem")
STOCK = {
    "PVE_URL": "https://pve.example.invalid:8006",
    "PVE_CLUSTER_ID": "lab",
    "PVE_NODE": "pve-node-a",
}


class PveSetupError(Exception):
    """Safe-to-display setup error without configuration values."""


def _safe_label(value):
    value = re.sub(r"[^A-Za-z0-9_.-]+", "-", (value or "").strip()).strip("-.")
    return value[:128] or "pve-node"


def _cluster_name():
    try:
        result = subprocess.run(["pvecm", "status"], capture_output=True, text=True, timeout=3, check=False)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode == 0:
        match = re.search(r"^Name:\s*(\S+)\s*$", result.stdout, re.MULTILINE)
        if match:
            return _safe_label(match.group(1))
    return None


def _read_values(path):
    values = {}
    if not path.exists():
        return values
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            stripped = line.strip()
            if not stripped or stripped.startswith("#") or "=" not in stripped:
                continue
            key, value = stripped.split("=", 1)
            values[key.strip()] = value.strip().strip("\"'")
    except OSError:
        raise PveSetupError("PVE configuration is unreadable") from None
    return values


def prepare(config_file, template_file, ca_source="/etc/pve/pve-root-ca.pem", hostname=None, cluster_name=None,
            node_name=None, default_ca=DEFAULT_CA):
    config_file = Path(config_file)
    template_file = Path(template_file)
    ca_source = Path(ca_source)
    default_ca = Path(default_ca)
    hostname = _safe_label(hostname or socket.getfqdn() or socket.gethostname())
    node_name = _safe_label(node_name or socket.gethostname().split(".", 1)[0])
    cluster_name = _safe_label(cluster_name or _cluster_name() or hostname)
    current = _read_values(config_file if config_file.exists() else template_file)
    if config_file.exists():
        original = config_file.read_text(encoding="utf-8")
    else:
        original = template_file.read_text(encoding="utf-8")

    lines = original.splitlines()
    seen = set()
    updates = {
        "PVE_URL": f"https://{hostname}:8006",
        "PVE_CLUSTER_ID": cluster_name,
        "PVE_NODE": node_name,
    }
    stock_config = (not config_file.exists() or current.get("PVE_URL") == STOCK["PVE_URL"]
                    or current.get("PVE_NODE") == STOCK["PVE_NODE"]
                    or current.get("PVE_TOKEN_SECRET", "").lower().startswith("replace-with-"))
    output = []
    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            output.append(line)
            continue
        key, value = stripped.split("=", 1)
        key, value = key.strip(), value.strip().strip("\"'")
        seen.add(key)
        if key == "PVE_CA_FILE" and current.get(key) in {str(DEFAULT_CA), str(LOCAL_CA)}:
            output.append(f"PVE_CA_FILE={default_ca}")
        elif key in updates and (key not in current or (value == STOCK[key] and (key == "PVE_URL" or stock_config))):
            output.append(f"{key}={updates[key]}")
        else:
            output.append(line)
    for key in ("PVE_URL", "PVE_CLUSTER_ID", "PVE_NODE"):
        if key not in seen:
            output.append(f"{key}={updates[key]}")
    if "PVE_CA_FILE" not in seen:
        output.append(f"PVE_CA_FILE={default_ca}")

    config_file.parent.mkdir(parents=True, exist_ok=True)
    config_file.write_text("\n".join(output).rstrip() + "\n", encoding="utf-8")
    config_file.chmod(0o600)

    values = _read_values(config_file)
    ca_target = Path(values.get("PVE_CA_FILE", str(DEFAULT_CA)))
    if ca_target == default_ca:
        if ca_target.parent.is_symlink():
            raise PveSetupError("PVE validation failed: default CA directory must not be a symbolic link")
        ca_target.parent.mkdir(parents=True, exist_ok=True)
        ca_target.parent.chmod(0o755)
        if ca_target.is_symlink():
            if not ca_source.is_file():
                raise PveSetupError("PVE sender is not installed yet: local trusted CA /etc/pve/pve-root-ca.pem is unavailable")
            ca_target.unlink()
            shutil.copyfile(ca_source, ca_target)
        if not ca_target.exists():
            if not ca_source.is_file():
                raise PveSetupError("PVE sender is not installed yet: local trusted CA /etc/pve/pve-root-ca.pem is unavailable")
            shutil.copyfile(ca_source, ca_target)
        ca_target.chmod(0o644)
    return config_file


def prepare_local(config_file, node_name=None, cluster_name=None):
    """Preserve existing identity and secrets while filling missing local identity."""
    config_file = Path(config_file)
    if config_file.is_symlink() or config_file.parent.is_symlink():
        raise PveSetupError("untrusted PVE configuration path")
    node = (node_name if node_name is not None else socket.gethostname()).split(".", 1)[0]
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9.-]{0,127}", node):
        raise PveSetupError("invalid local PVE node name")
    cluster = _safe_label(cluster_name or node)
    values = _read_values(config_file)
    if values.get("PVE_NODE") and values["PVE_NODE"] != node:
        raise PveSetupError("PVE_NODE differs from the local node; correct PVE_NODE before installation")
    original = config_file.read_text(encoding="utf-8") if config_file.exists() else ""
    missing = [(key, value) for key, value in (("PVE_NODE", node), ("PVE_CLUSTER_ID", cluster))
               if not values.get(key)]
    if missing and any(key in values for key, _ in missing):
        raise PveSetupError("PVE identity is empty")
    config_file.parent.mkdir(parents=True, exist_ok=True)
    if missing or not config_file.exists():
        if original and not original.endswith("\n"):
            original += "\n"
        fd, name = tempfile.mkstemp(prefix=".pve-config-", dir=config_file.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as output:
                output.write(original + "".join(f"{key}={value}\n" for key, value in missing))
            os.replace(name, config_file)
        finally:
            Path(name).unlink(missing_ok=True)
    config_file.chmod(0o600)
    return config_file


def _dynamic_user_can_read(path):
    path = Path(path)
    try:
        resolved = path.resolve(strict=True)
        if not resolved.is_file() or resolved.stat().st_mode & 0o004 == 0:
            return False
        for parent in set(path.absolute().parents) | set(resolved.parents):
            if parent.stat().st_mode & 0o001 == 0:
                return False
    except OSError:
        return False
    return True


def preflight(config_file, client_factory=PveApiClient):
    config_file = Path(config_file)
    values = _read_values(config_file)
    secret = values.get("PVE_TOKEN_SECRET", "")
    if not secret or secret.lower().startswith(("replace-with-", "placeholder", "change-me")):
        raise PveSetupError("PVE sender is not installed yet: replace the PVE_TOKEN_SECRET placeholder with a real read-only API token secret")
    token_id = values.get("PVE_TOKEN_ID", "")
    if not token_id:
        raise PveSetupError("PVE sender is not installed yet: set a valid PVE_TOKEN_ID")
    if not _dynamic_user_can_read(values.get("PVE_CA_FILE", "")):
        raise PveSetupError("PVE validation failed: CA file and parent directories must be readable by systemd DynamicUser")
    try:
        config = PveConfig.from_environment(env={}, env_file=str(config_file))
    except PveError as error:
        raise PveSetupError(f"PVE validation failed: {error}") from None
    if config is None:
        raise PveSetupError("PVE sender is not installed yet: complete the PVE token and node settings")
    try:
        client_factory(config).validate_scope()
    except PveError as error:
        raise PveSetupError(f"PVE validation failed: {error}") from None
    return config


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True)
    parser.add_argument("--template", required=True)
    parser.add_argument("--local", action="store_true")
    args = parser.parse_args(argv)
    try:
        config = Path(args.config)
        if args.local:
            prepare_local(config)
        else:
            provision_token(config, prepare_config=lambda: prepare(args.config, args.template))
            preflight(config)
    except (PveSetupError, PveTokenError) as error:
        print(str(error), file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
