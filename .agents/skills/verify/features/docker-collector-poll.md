# Docker collector polling

Docker collector polling invokes the configured Docker CLI (`DOCKER_CLI_PATH`) to collect container state, CPU percentage, and memory usage into a bounded JSON snapshot, capping at 30 containers and failing closed to `status: "error"` without leaking daemon stderr.

## Sub-features

- `docker-poll-ok` parses running and exited containers, computing memory percentage only for running containers.
- `docker-poll-truncated` caps output at 30 containers and sets `truncated: true` when more than 30 exist.
- `docker-poll-error` fails closed with `status: "error"`, empty containers list, and no raw daemon stderr in the payload.

## How to get to it (user POV)

- Set `DOCKER_CLI_PATH` to the Docker executable (or fixture shim) and call `hwmonitor_mqtt.collectors.docker.poll_once(node=...)`.

## Driving it with verify_agent

Preconditions:

- `verify_agent.py doctor --run-dir $RUN_DIR` returns `"ok": true`.
- `$DOCKER_SHIM` is the `state.shims.docker` path from `launch` (`$RUN_DIR\shims\fake-docker.cmd` on Windows or `$RUN_DIR/shims/fake-docker` on POSIX).

- **Normal poll.** Drive `poll_once` with the shim in default `ok` mode. Run `& $PY .agents/skills/verify/scripts/verify_agent.py drive --run-dir $RUN_DIR --feature docker-collector-poll --entry poll-ok --env "DOCKER_CLI_PATH=$DOCKER_SHIM" --env "VERIFY_DOCKER_MODE=ok" -- $PY -c "import json; from hwmonitor_mqtt.collectors.docker import poll_once; print(json.dumps(poll_once('verify-node'), indent=2))"`. Exit code is `0`; stdout JSON has `"status": "ok"`, `"truncated": false`, 2 containers (`mqtt-broker` with `cpu_percent: 4.5` and `memory_usage_bytes: 134217728`; `backup-job` with `cpu_percent: null` and `memory_usage_bytes: null`).
- **Truncated poll.** Drive `poll_once` with 32 containers. Run `& $PY .agents/skills/verify/scripts/verify_agent.py drive --run-dir $RUN_DIR --feature docker-collector-poll --entry poll-truncated --env "DOCKER_CLI_PATH=$DOCKER_SHIM" --env "VERIFY_DOCKER_MODE=truncated" -- $PY -c "import json; from hwmonitor_mqtt.collectors.docker import poll_once; r = poll_once('verify-node'); print(json.dumps({'status': r['status'], 'count': len(r['containers']), 'truncated': r['truncated']}))"`. Exit code is `0`; stdout JSON shows `{"status": "ok", "count": 30, "truncated": true}`.
- **Daemon error fail-closed.** Drive `poll_once` when the Docker CLI exits non-zero. Run `& $PY .agents/skills/verify/scripts/verify_agent.py drive --run-dir $RUN_DIR --feature docker-collector-poll --entry poll-error --env "DOCKER_CLI_PATH=$DOCKER_SHIM" --env "VERIFY_DOCKER_MODE=error" -- $PY -c "import json; from hwmonitor_mqtt.collectors.docker import poll_once; print(json.dumps(poll_once('verify-node'), indent=2))"`. Exit code is `0`; stdout JSON has `"status": "error"`, `"containers": []`, `"truncated": false`, and no `"Cannot connect"` error string in stdout.

## Gotchas

- Never unset `DOCKER_CLI_PATH` during verification on a developer machine with Docker Desktop installed, or `poll_once()` will query real local containers.
- Exited containers intentionally drop stats (`cpu_percent: null`, `memory_usage_bytes: null`) even if `docker stats --all` emits a row for them.
