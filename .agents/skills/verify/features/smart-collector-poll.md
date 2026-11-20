# SMART collector polling

SMART collector polling invokes `smartctl --scan -j` and `smartctl -a -j <dev>` to normalize disk health metrics (including NVMe Kelvin-to-Celsius conversion) and evaluate warning/critical alerts, degrading cleanly to `status: "unavailable"` when `smartctl` fails.

## Sub-features

- `smart-poll-healthy` scans and normalizes a healthy NVMe disk, converting 310 K to 36.85 C and reporting `status: "ok"`.
- `smart-poll-critical` detects a failing NVMe disk (`passed: false`, `critical_warning: 1`, `media_errors: 3`, high temp, low spare, 100% wear) and reports `status: "critical"` with structured alerts.
- `smart-poll-error` degrades to `status: "unavailable"` with empty `devices` and `alerts` when `smartctl` fails.

## How to get to it (user POV)

- Instantiate `hwmonitor_mqtt.collectors.smart.SmartCollector(smartctl=<path>)` and call `poll_once()`.

## Driving it with verify_agent

Preconditions:

- `verify_agent.py doctor --run-dir $RUN_DIR` returns `"ok": true`.
- `$SMART_SHIM` is the `state.shims.smartctl` path from `launch` (`$RUN_DIR\shims\fake-smartctl.cmd` on Windows or `$RUN_DIR/shims/fake-smartctl` on POSIX).

- **Healthy NVMe poll.** Drive `SmartCollector` with the shim in `healthy` mode. Run `& $PY .agents/skills/verify/scripts/verify_agent.py drive --run-dir $RUN_DIR --feature smart-collector-poll --entry poll-healthy --env "VERIFY_SMART_MODE=healthy" -- $PY -c "import json, sys; from hwmonitor_mqtt.collectors.smart import SmartCollector; print(json.dumps(SmartCollector(smartctl=sys.argv[1]).poll_once(now=1700000000), indent=2))" "$SMART_SHIM"`. Exit code is `0`; stdout JSON has `"status": "ok"`, `"alerts": []`, and `devices["/dev/nvme0"]["critical_metrics"]["temperature_c"]` approximately `36.85`.
- **Critical NVMe poll.** Drive `SmartCollector` with the shim in `critical` mode. Run `& $PY .agents/skills/verify/scripts/verify_agent.py drive --run-dir $RUN_DIR --feature smart-collector-poll --entry poll-critical --env "VERIFY_SMART_MODE=critical" -- $PY -c "import json, sys; from hwmonitor_mqtt.collectors.smart import SmartCollector; r = SmartCollector(smartctl=sys.argv[1]).poll_once(now=1700000000); print(json.dumps({'status': r['status'], 'device_status': r['devices']['/dev/nvme0']['status'], 'alert_metrics': [a['metric'] for a in r['alerts']]}, indent=2))" "$SMART_SHIM"`. Exit code is `0`; stdout JSON has `"status": "critical"`, `"device_status": "critical"`, and `alert_metrics` containing `"overall_passed"`, `"uncorrectable_errors"`, `"wear_percent"`, `"spare_percent"`, and `"temperature_c"`.
- **Failing smartctl fallback.** Drive `SmartCollector` when `smartctl` exits non-zero. Run `& $PY .agents/skills/verify/scripts/verify_agent.py drive --run-dir $RUN_DIR --feature smart-collector-poll --entry poll-error --env "VERIFY_SMART_MODE=error" -- $PY -c "import json, sys; from hwmonitor_mqtt.collectors.smart import SmartCollector; print(json.dumps(SmartCollector(smartctl=sys.argv[1]).poll_once(now=1700000000), indent=2))" "$SMART_SHIM"`. Exit code is `0`; stdout JSON has `"status": "unavailable"`, `"alerts": []`, and `"devices": {}`.

## Gotchas

- `SmartCollector._smartctl_devices()` checks `shutil.which(self.smartctl)`. On Windows, the shim MUST have a `.cmd` extension (which `verify_agent.py launch` creates automatically) so `shutil.which()` resolves it as executable.
- Pass an explicit `now=1700000000` timestamp when capturing deterministic evidence; omitting `now` uses `time.time()`.
