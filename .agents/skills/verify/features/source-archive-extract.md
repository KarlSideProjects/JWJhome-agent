# Source archive extraction

Source archive extraction unpacks a bounded GitHub `.tar.gz` source archive into a fresh staging directory, returning the extracted root directory path while rejecting path traversal, duplicate entries, non-file/non-directory members, and multi-root archives.

## Sub-features

- `archive-extract-valid` extracts a valid single-root `.tar.gz` archive into a new destination directory.
- `archive-confirm-files` confirms extracted files match expected contents on disk.
- `archive-reject-traversal` rejects an archive containing `../` path traversal without leaving an extracted destination behind.

## How to get to it (user POV)

- Run `python scripts/source_archive.py <ARCHIVE> <NEW_DESTINATION>` in a terminal.

## Driving it with verify_agent

Preconditions:

- `verify_agent.py doctor --run-dir $RUN_DIR` returns `"ok": true`.
- `$RUN_DIR\archives\valid-source.tar.gz` and `$RUN_DIR\archives\traversal-source.tar.gz` exist.
- `$RUN_DIR\staging\extracted-ok` and `$RUN_DIR\staging\extracted-bad` do not yet exist.

- **Extract valid archive.** Unpack the seeded valid archive. Run `& $PY .agents/skills/verify/scripts/verify_agent.py drive --run-dir $RUN_DIR --feature source-archive-extract --entry extract-valid -- $PY scripts/source_archive.py "$RUN_DIR\archives\valid-source.tar.gz" "$RUN_DIR\staging\extracted-ok"`. Exit code is `0`; stdout prints the extracted root path ending in `extracted-ok\JWJhome-agent-verify`.
- **Confirm extracted files.** Inspect the extracted files in a read-only second step. Run `& $PY .agents/skills/verify/scripts/verify_agent.py drive --run-dir $RUN_DIR --feature source-archive-extract --entry confirm-files -- $PY -c "from pathlib import Path; import sys; root = Path(sys.argv[1]) / 'JWJhome-agent-verify'; print(sorted(p.name for p in root.iterdir()), (root / 'README.md').read_text(encoding='utf-8').strip())" "$RUN_DIR\staging\extracted-ok"`. Exit code is `0`; stdout shows `['README.md', 'run.sh'] # Verified source fixture`.
- **Reject traversal archive.** Attempt to unpack the traversal archive. Run `& $PY .agents/skills/verify/scripts/verify_agent.py drive --run-dir $RUN_DIR --feature source-archive-extract --entry reject-traversal -- $PY scripts/source_archive.py "$RUN_DIR\archives\traversal-source.tar.gz" "$RUN_DIR\staging\extracted-bad"`. Exit code is non-zero (`1`); stderr contains `ValueError: unsafe archive member`.
- **Confirm no escape or leftover dir.** Check that neither the bad destination nor an escaped file was created. Run `& $PY .agents/skills/verify/scripts/verify_agent.py drive --run-dir $RUN_DIR --feature source-archive-extract --entry confirm-no-escape -- $PY -c "from pathlib import Path; import sys; base = Path(sys.argv[1]); print({'bad_exists': (base / 'extracted-bad').exists(), 'escape_exists': (base / 'escape.txt').exists()})" "$RUN_DIR\staging"`. Exit code is `0`; stdout shows `{'bad_exists': False, 'escape_exists': False}`.

## Gotchas

- `scripts/source_archive.py` requires `<NEW_DESTINATION>` not to exist beforehand (`destination.mkdir(mode=0o700)` raises `FileExistsError` if reused). Use a fresh subdirectory under `$RUN_DIR\staging` for each attempt.
- Windows ignores POSIX mode bits (`0o700`/`0o755`), so permission-bit checks apply only on POSIX hosts.
