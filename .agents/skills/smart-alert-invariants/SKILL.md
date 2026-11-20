---
name: smart-alert-invariants
description: "Formally verify SMART disk-health normalization, Kelvin-to-Celsius conversion, failure-status precedence, alert severity dominance, and CRC delta monotonicity in hwmonitor_mqtt/collectors/smart.py using Z3 SMT and Hypothesis. Use whenever editing hwmonitor_mqtt/collectors/smart.py."
---

# SMART Alert Invariants

Formally checks the safety properties of `JWJhome-agent`'s disk SMART collector so that failing disks cannot be silently downgraded to `"ok"` or `"unknown"`, Kelvin temperatures cannot trigger false overheat alarms, and malformed vendor fields cannot leak negative/non-finite numbers or internal scratch keys.

## Coverage Record

- **Sync commit:** `9fbac989359eacbc467182100800cd95735847c0`
- **Covered file:** `hwmonitor_mqtt/collectors/smart.py`
- **Covered symbols:**
  - `_SEVERITY` (lattice ordering)
  - `_number`
  - `_parse_health_status`
  - `_pve_smart_to_json`
  - `_normalize_device`
  - `_evaluate`
  - `SmartCollector.poll_once`

Move the sync commit forward **only** after confirming that `scripts/verify_smart_invariants.py` still describes the code at the new commit and passes cleanly.

## Properties & Harms

1. **Critical threshold dominance (Z3 SMT over all reals):** For both high-bound (`value >= crit`) and low-bound (`value <= crit`) thresholds, critical severity strictly dominates warning severity even if thresholds are equal or inverted.
   - *Harm prevented:* A misconfigured or tight threshold pair masking a critical disk failure as a warning or ok.
2. **Numeric domain safety (Hypothesis):** `_number` never returns a negative number, `NaN`, or `+inf` for any input type.
   - *Harm prevented:* `NaN` comparisons silently bypassing threshold checks or producing invalid JSON payloads.
3. **Health-status failure dominance (Hypothesis):** If either `health_str` or `text_str` contains a whole-word failure token (`FAILED`, `FAIL`, `BAD`), `_parse_health_status` returns `False` even if the other string contains `PASSED` or `OK`.
   - *Harm prevented:* Contradictory PVE/smartctl text hiding a failed disk self-assessment.
4. **Critical failure non-suppression & status soundness (Hypothesis):** Any critical SMART condition (`overall_passed=False`, `critical_warning != 0`, `when_failed="FAILING_NOW"`, `unstable_sectors > 0`, `uncorrectable_errors > 0`, `reallocated_sectors >= 10`, `wear_percent >= 100`, `spare_percent <= threshold`, or `temperature_c >= crit_temp`) emits at least one `"critical"` alert, sets `device["status"] == "critical"`, matches `max(alert severities)`, and strips temporary keys (`available_spare_threshold`, `crc_delta`).
   - *Harm prevented:* Silent data loss from an unalerted failing disk or schema pollution from leaked internal keys.
5. **Kelvin-to-Celsius normalization (Hypothesis):** Normal NVMe Kelvin temperatures (`273.15..330.0 K`) convert to Celsius (`0..56.85 C`) in both `_normalize_device` and `_pve_smart_to_json` without triggering false temperature alerts.
   - *Harm prevented:* Spurious critical overheat alerts on healthy NVMe drives reporting Kelvin.
6. **Host aggregation & CRC baseline monotonicity (Hypothesis):** `SmartCollector.poll_once` sets host `status` to `"unavailable"` when no devices exist and to `max(device statuses)` otherwise, flattens all device alerts, and emits an `interface_crc_errors` warning if and only if the error count increases above the first-observed baseline.
   - *Harm prevented:* A single failing disk being hidden behind healthy sibling disks, or counter resets firing spurious CRC warnings.

## Run Command

Run from the `JWJhome-agent` repository root:

```powershell
python .agents/skills/smart-alert-invariants/scripts/verify_smart_invariants.py
```

- Override example count if desired: `SMART_VERIFY_EXAMPLES=500` (default: `200`, derandomized for reproducibility).
- Tested with Python 3.12.10, `hypothesis 6.170.15`, and `z3-solver 5.4.1`.

## Scope & Bounds

- **Verified directly on real code:** All 5 Hypothesis properties call the real functions in `hwmonitor_mqtt/collectors/smart.py` with `SMART_*` environment overrides cleared to default thresholds.
- **Verified symbolically (unbounded reals):** The Z3 proof checks the exact `check()` classification formula and `_SEVERITY` ordering over all real numbers, not just sampled floats.
- **Not covered by this verifier:**
  - Subprocess invocation of `smartctl` (`_smartctl_devices`) and PVE snapshot file-permission / partial-retention logic (`_local_poll`). Use `.agents/skills/verify/` and `tests/test_smart_collector.py` for those.
  - Custom `SMART_*` environment variable overrides (cleared during property runs to test default thresholds deterministically).
  - Concurrent mutation of a single `SmartCollector` instance across threads.

## Interpreting Failures

- **Failing Hypothesis assertion:** Hypothesis prints the minimal shrinking counterexample (`Falsifying example: ...`). Read the concrete input fields directly in `smart.py`'s terms.
  - If the change was **unintended** (e.g., a broken threshold comparison, dropped alert, or skipped Kelvin conversion), fix `hwmonitor_mqtt/collectors/smart.py` and rerun.
  - If the change **intentionally** altered a default threshold or schema contract, update the corresponding property in `scripts/verify_smart_invariants.py`, rerun until green, and update the Coverage Record sync commit.
- **Failing Z3 check:** Z3 prints a concrete real-valued model `(value, warn, crit)` that violates threshold dominance. Either `_SEVERITY` ordering changed or `check()` in `_evaluate()` no longer matches the modeled formula.
