# JWJhome-agent verification map

This directory is the maintained source for verifying the locally executable CLI and collector surfaces of `JWJhome-agent`. Read this index before driving the repo, then follow the matching feature file.

## Baseline preconditions

- Use Python 3.12 (`$PY = "C:\Users\Orson\AppData\Local\Programs\Python\Python312\python.exe"` on Windows, or `python3` on Linux/macOS).
- Run `verify_agent.py launch --run-dir $RUN_DIR --evidence-dir $EVIDENCE_DIR` with two non-overlapping paths.
- Run `verify_agent.py doctor --run-dir $RUN_DIR` and require `"ok": true`.
- Never drive a run directory that was not initialized by this verification run.

## Driving conventions

- Start each recipe from a clean launched run directory unless its preconditions say otherwise.
- Treat every command as literal and keep flags and quotes unchanged.
- Execute every verification command through `verify_agent.py drive --run-dir $RUN_DIR --feature <feature> --entry <entry> -- <command...>`.
- Pass external CLI boundaries via `--env KEY=VALUE` instead of mutating the parent shell.
- Never remove `$EVIDENCE_DIR` during cleanup.

## Proof and skip reporting

- Capture both the user action and its observable side effect or refusal.
- CLI proof includes the literal command, environment overrides, exit code, stdout, and stderr.
- Filesystem-mutating commands (such as archive extraction) require a read-only second check confirming the resulting files or absence of escaped files.
- Record the feature ID and entry point in every artifact path (`<evidence-dir>/<feature>/<entry>.json`).
- Report any unreachable path with the attempted command and unmet precondition; never report a skipped path as verified through a different path.

## Feature entry contract

Each feature file starts with an H1 title and one paragraph describing the user-visible behavior, followed by exactly four H2 sections in this order:

1. `Sub-features` lists short IDs with one line for each behavior.
2. `How to get to it (user POV)` lists every user entry point.
3. `Driving it with verify_agent` starts with `Preconditions:` and uses labeled bullets pairing each action with an exact command and observable result.
4. `Gotchas` lists traps that can waste or invalidate a verification run.

## Features

- [Platform installer dry-run](./platform-install-dry-run.md) covers host detection, collector dispatch, command preview, and invalid override rejection.
- [Source archive extraction](./source-archive-extract.md) covers safe bounded `.tar.gz` staging and path-traversal rejection.
- [Docker collector polling](./docker-collector-poll.md) covers container/stats parsing, 30-container truncation, and daemon-error fail-closed behavior.
- [SMART collector polling](./smart-collector-poll.md) covers healthy NVMe normalization, critical failure alerting, and missing/failing `smartctl` fallback.
