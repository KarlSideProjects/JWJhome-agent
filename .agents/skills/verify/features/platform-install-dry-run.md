# Platform installer dry-run

Platform installer dry-run detects the current host OS and runtime, selects the matching sender collector and platform installer command, prints the decision as JSON without installing anything, and rejects contradictory flags.

## Sub-features

- `install-dry-run-default` detects the current host and previews the collector and installer command.
- `install-dry-run-custom-root` resolves installer script paths relative to `--project-root`.
- `install-reject-target-override` rejects `--target-system` when running on a native host rather than inside a container.
- `install-reject-docker-on-windows` rejects `--deployment docker` on a non-Linux host.

## How to get to it (user POV)

- Run `python -m hwmonitor_mqtt.platforms.install --dry-run` from the repository root.
- Run `python -m hwmonitor_mqtt.platforms.install --dry-run --project-root <dir>` to preview paths for another checkout.
- Run `python -m hwmonitor_mqtt.platforms.install --dry-run --target-system linux` or `--deployment docker` to verify guardrails.

## Driving it with verify_agent

Preconditions:

- `verify_agent.py doctor --run-dir $RUN_DIR` returns `"ok": true`.
- `$RUN_DIR` and `$EVIDENCE_DIR` are disjoint paths.

- **Default dry-run.** Preview the native host plan. Run `& $PY .agents/skills/verify/scripts/verify_agent.py drive --run-dir $RUN_DIR --feature platform-install-dry-run --entry default-dry-run -- $PY -m hwmonitor_mqtt.platforms.install --dry-run`. Exit code is `0`; stdout JSON has `"role": "sender"`, `"ready": true`, `"collector": "hwmonitor_mqtt.agents.agent_sender_windows"` (on Windows), and a `command` pointing to `scripts/windows/install_windows_sender_task.ps1`.
- **Custom project root.** Preview with a custom root path. Run `& $PY .agents/skills/verify/scripts/verify_agent.py drive --run-dir $RUN_DIR --feature platform-install-dry-run --entry custom-root -- $PY -m hwmonitor_mqtt.platforms.install --dry-run --project-root $RUN_DIR`. Exit code is `0`; stdout JSON `command` references `$RUN_DIR\scripts\windows\install_windows_sender_task.ps1`.
- **Reject contradictory target override.** Pass `--target-system linux` on a native host. Run `& $PY .agents/skills/verify/scripts/verify_agent.py drive --run-dir $RUN_DIR --feature platform-install-dry-run --entry reject-target-override -- $PY -m hwmonitor_mqtt.platforms.install --dry-run --target-system linux`. Exit code is `2`; stdout JSON contains `"status": "Explicit target contradicts detected host"`.
- **Reject Docker deployment on Windows.** Pass `--deployment docker` on Windows. Run `& $PY .agents/skills/verify/scripts/verify_agent.py drive --run-dir $RUN_DIR --feature platform-install-dry-run --entry reject-docker-windows -- $PY -m hwmonitor_mqtt.platforms.install --dry-run --deployment docker`. Exit code is `2`; stdout JSON contains `"status": "Docker deployment requires a supported Linux host"`.
- **Verify zero side effects.** Confirm `--dry-run` did not create files in `$RUN_DIR\scripts` or modify the repo. Run `& $PY .agents/skills/verify/scripts/verify_agent.py drive --run-dir $RUN_DIR --feature platform-install-dry-run --entry verify-no-side-effects -- $PY -c "from pathlib import Path; import sys; p = Path(sys.argv[1]) / 'scripts'; print({'scripts_exists': p.exists()})" $RUN_DIR`. Exit code is `0` and stdout shows `{'scripts_exists': False}`.

## Gotchas

- Never omit `--dry-run`; without it, `hwmonitor_mqtt.platforms.install` executes the real PowerShell or Docker installer command on the host.
- On Linux hosts, `reject-docker-windows` does not apply because Linux supports `--deployment docker`; report that entry point as skipped with the unmet OS precondition rather than marking it passed.
- On Windows hosts, `target.machine` reflects the real local hostname and may contain non-ASCII Unicode escapes in JSON output.
