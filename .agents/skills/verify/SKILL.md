---
name: verify
description: "Drive and verify JWJhome-agent's locally executable Python CLIs and fixture-bounded collectors (platform installer dry-run, source archive extractor, Docker CLI collector, SMART collector) in an isolated disposable run directory with preserved proof artifacts. Use after modifying hwmonitor_mqtt/platforms/, hwmonitor_mqtt/collectors/, or scripts/source_archive.py."
---

# Verify JWJhome-agent

Drive the real `JWJhome-agent` Python CLI entry points and boundary-isolated collectors without requiring an external MQTT broker, Docker daemon, or `smartctl` hardware, while capturing reproducible proof artifacts outside the disposable run directory.

> **Other surfaces not covered by this harness:**
> - Live telemetry senders (`hwmonitor_mqtt/agents/agent_sender_windows.py`, `hwmonitor_mqtt/agents/agent_sender_async.py`) require `psutil`, `paho-mqtt`, and a reachable MQTT broker (plus LibreHardwareMonitor / PawnIO on Windows).
> - Captive portal (`wifi_portal/webui/app.py`) requires `fastapi`, `uvicorn`, and Linux `wpa_supplicant`.
> - Viewers (`hwmonitor_mqtt/viewers/tui_viewer.py`, `pygame_viewer.py`) require `textual` or `pygame` and an interactive display/broker.

## Launch

This repo's verified surfaces are short-lived Python CLIs and collectors (no background server to keep alive). Launch initializes a disposable run directory containing seeded fixture archives and boundary shims (`fake-docker`, `fake-smartctl`), plus a separate evidence directory outside the run directory.

Choose distinct paths per run (for example using a unique `$RUN_ID` under your temp folder) so concurrent runs never share state:

```powershell
$PY = "python" # or "$env:LOCALAPPDATA\Programs\Python\Python312\python.exe" on Windows if not on PATH
$RUN_ID = [Guid]::NewGuid().ToString("N").Substring(0, 8)
$RUN_DIR = Join-Path $env:TEMP "jwjhome-agent-run-$RUN_ID"
$EVIDENCE_DIR = Join-Path $env:TEMP "jwjhome-agent-evidence-$RUN_ID"

& $PY .agents/skills/verify/scripts/verify_agent.py launch --run-dir $RUN_DIR --evidence-dir $EVIDENCE_DIR
```

**Ready signal:** Exit code `0` and JSON output with `"ok": true` listing `state.shims.docker`, `state.shims.smartctl`, `state.archives.valid`, and `state.archives.traversal`.

## Doctor

Run this read-only check before driving any feature, and whenever anything looks off:

```powershell
& $PY .agents/skills/verify/scripts/verify_agent.py doctor --run-dir $RUN_DIR
```

Doctor verifies without mutating state:
- `state.json` exists in `$RUN_DIR`.
- `$EVIDENCE_DIR` exists and does not overlap `$RUN_DIR`.
- Current repo root and `git rev-parse HEAD` match the launched snapshot.
- Seeded shims and fixture archives exist.
- Python imports `hwmonitor_mqtt.platforms.install`, `hwmonitor_mqtt.collectors.docker`, and `hwmonitor_mqtt.collectors.smart` cleanly.

Do not drive a run directory whose `doctor` output has `"ok": false`.

## Drive

Read [`features/README.md`](./features/README.md) first, then follow the matching feature file in [`features/`](./features/). Every drive runs a literal command through `verify_agent.py drive` and records `{feature, entry_point, timestamp, git_head, cwd, env_overrides, command, exit_code, stdout, stderr}`:

```powershell
# Example: platform installer dry-run
& $PY .agents/skills/verify/scripts/verify_agent.py drive `
  --run-dir $RUN_DIR `
  --feature platform-install-dry-run `
  --entry default-dry-run `
  -- $PY -m hwmonitor_mqtt.platforms.install --dry-run
```

Pass `--env KEY=VALUE` (repeatable) to inject production boundary variables such as `DOCKER_CLI_PATH` or shim modes without polluting the parent shell environment.

## Evidence

- **Location:** `<evidence-dir>/<feature>/<entry>.json` (strictly outside `<run-dir>`).
- **Proof standards:**
  - Exercise the real CLI entry point or production boundary (`python -m hwmonitor_mqtt.platforms.install`, `scripts/source_archive.py`, `DOCKER_CLI_PATH`, `SmartCollector(smartctl=...)`), not internal private setters or test-only hooks.
  - Capture the exact command, exit code, stdout, and stderr for both the action and any follow-up verification step (for example checking extracted files on disk after `source_archive.py` runs).
  - Verify what `--dry-run` skips by confirming `git status -s` and the target directory remain untouched rather than assuming `--dry-run` is side-effect-free.
  - Mocks are permitted only at external CLI boundaries that production already isolates (`DOCKER_CLI_PATH` and `SmartCollector(smartctl=...)`).

## Cleanup

Delete the disposable run directory while preserving all captured evidence:

```powershell
& $PY .agents/skills/verify/scripts/verify_agent.py cleanup --run-dir $RUN_DIR
```

- `cleanup` removes only `$RUN_DIR` (refusing if `$EVIDENCE_DIR` is inside `$RUN_DIR`).
- Never kill processes by name; this harness spawns only short-lived child commands that exit before `drive` returns.
- Confirm `"run_dir_exists": false`, `"evidence_dir_exists": true`, and non-empty `"surviving_artifacts"` in the cleanup JSON output.
- Always run `cleanup` after failed iterations too so scratch directories do not accumulate.

## Helpers

- [`.agents/skills/verify/scripts/verify_agent.py`](./scripts/verify_agent.py): stdlib-only Python CLI with four subcommands:
  - `launch --run-dir <dir> --evidence-dir <dir>`
  - `doctor --run-dir <dir>`
  - `drive --run-dir <dir> --feature <id> --entry <name> [--cwd <dir>] [--env K=V]... -- <cmd...>`
  - `cleanup --run-dir <dir>`
