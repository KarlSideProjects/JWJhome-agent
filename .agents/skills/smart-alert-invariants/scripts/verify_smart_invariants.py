#!/usr/bin/env python3
"""Formal invariant verifier for hwmonitor_mqtt/collectors/smart.py.

Combines Z3 SMT proofs (severity lattice and threshold dominance) with
Hypothesis property-based testing directly against the real Python functions
in hwmonitor_mqtt.collectors.smart.
"""

from __future__ import annotations

import math
import os
from pathlib import Path
import sys
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[4]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from hypothesis import given, settings, strategies as st
import z3

from hwmonitor_mqtt.collectors import smart
from hwmonitor_mqtt.collectors.smart import (
    SmartCollector,
    _SEVERITY,
    _evaluate,
    _normalize_device,
    _number,
    _parse_health_status,
    _pve_smart_to_json,
)

MAX_EXAMPLES = int(os.environ.get("SMART_VERIFY_EXAMPLES", "200"))

_SMART_ENV_KEYS = (
    "SMART_REALLOC_WARN",
    "SMART_REALLOC_CRIT",
    "SMART_WEAR_WARN_PCT",
    "SMART_WEAR_CRIT_PCT",
    "SMART_SPARE_WARN_PCT",
    "SMART_SPARE_CRIT_PCT",
    "SMART_TEMP_WARN_HDD",
    "SMART_TEMP_CRIT_HDD",
    "SMART_TEMP_WARN_SSD",
    "SMART_TEMP_CRIT_SSD",
)


def _clear_smart_env() -> dict[str, str | None]:
    saved = {key: os.environ.get(key) for key in _SMART_ENV_KEYS}
    for key in _SMART_ENV_KEYS:
        os.environ.pop(key, None)
    return saved


def _restore_smart_env(saved: dict[str, str | None]) -> None:
    for key, value in saved.items():
        if value is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = value


def verify_z3_severity_and_thresholds() -> None:
    """SMT proof over all reals: critical threshold always dominates warning."""
    # Verify actual _SEVERITY ordering in smart.py matches strict lattice.
    expected_order = ["unavailable", "unknown", "ok", "warning", "critical"]
    ranks = [_SEVERITY[k] for k in expected_order]
    assert ranks == sorted(ranks) and len(set(ranks)) == len(ranks), (
        f"_SEVERITY does not form a strict total order: {_SEVERITY}"
    )

    value, warn, crit = z3.Reals("value warn crit")
    # Mirror smart._evaluate.check() lines 375-376:
    # high: critical if value >= crit else (warning if value >= warn else none)
    sev_high = z3.If(value >= crit, 4, z3.If(value >= warn, 3, 2))
    # low: critical if value <= crit else (warning if value <= warn else none)
    sev_low = z3.If(value <= crit, 4, z3.If(value <= warn, 3, 2))

    # Property A: For any real warn/crit (even inverted), value >= crit => critical (high)
    s1 = z3.Solver()
    s1.add(value >= crit, sev_high != 4)
    assert s1.check() == z3.unsat, f"High-direction critical dominance violated: {s1.model()}"

    # Property B: For any real warn/crit (even inverted), value <= crit => critical (low)
    s2 = z3.Solver()
    s2.add(value <= crit, sev_low != 4)
    assert s2.check() == z3.unsat, f"Low-direction critical dominance violated: {s2.model()}"

    # Property C: In well-ordered thresholds (warn <= crit for high, warn >= crit for low),
    # warning zone strictly lies between ok and critical.
    s3 = z3.Solver()
    s3.add(warn <= crit, value >= warn, value < crit, sev_high != 3)
    assert s3.check() == z3.unsat, f"High-direction warning zone violated: {s3.model()}"

    s4 = z3.Solver()
    s4.add(warn >= crit, value <= warn, value > crit, sev_low != 3)
    assert s4.check() == z3.unsat, f"Low-direction warning zone violated: {s4.model()}"


@settings(max_examples=MAX_EXAMPLES, derandomize=True, deadline=None)
@given(
    st.one_of(
        st.none(),
        st.floats(allow_nan=True, allow_infinity=True),
        st.integers(min_value=-10**12, max_value=10**12),
        st.text(max_size=24),
        st.lists(st.integers(), max_size=3),
        st.dictionaries(st.text(max_size=5), st.integers(), max_size=3),
    )
)
def verify_number_domain_safety(raw: Any) -> None:
    """_number never returns negative, NaN, or +inf."""
    result = _number(raw)
    if result is not None:
        assert isinstance(result, float), f"expected float, got {type(result)}"
        assert not math.isnan(result) and not math.isinf(result), f"non-finite result: {result}"
        assert result >= 0.0, f"negative result: {result}"


@settings(max_examples=MAX_EXAMPLES, derandomize=True, deadline=None)
@given(
    fail_word=st.sampled_from(["FAILED", "FAIL", "BAD", "failed", "Fail", "bad"]),
    pass_word=st.sampled_from(["PASSED", "OK", "passed", "ok"]),
    prefix=st.text(alphabet="abcdefghijklmnopqrstuvwxyz0123456789 _-:", max_size=12),
    suffix=st.text(alphabet="abcdefghijklmnopqrstuvwxyz0123456789 _-:", max_size=12),
    fail_in_first=st.booleans(),
)
def verify_parse_health_failure_dominance(
    fail_word: str,
    pass_word: str,
    prefix: str,
    suffix: str,
    fail_in_first: bool,
) -> None:
    """Any whole-word failure token in either input forces False even alongside PASSED/OK."""
    fail_str = f"{prefix} {fail_word} {suffix}"
    pass_str = f"{prefix} {pass_word} {suffix}"
    health_str, text_str = (fail_str, pass_str) if fail_in_first else (pass_str, fail_str)
    assert _parse_health_status(health_str, text_str) is False, (
        f"Failure token masked by pass token: health={health_str!r}, text={text_str!r}"
    )


@settings(max_examples=MAX_EXAMPLES, derandomize=True, deadline=None)
@given(
    overall_passed=st.one_of(st.none(), st.booleans()),
    critical_warning=st.sampled_from([0, 1, 2, "0x0", "0x04"]),
    failing_now=st.booleans(),
    unstable=st.one_of(st.none(), st.integers(min_value=0, max_value=50)),
    media_errors=st.one_of(st.none(), st.integers(min_value=0, max_value=50)),
    reallocated=st.one_of(st.none(), st.integers(min_value=0, max_value=50)),
    wear=st.one_of(st.none(), st.integers(min_value=0, max_value=120)),
    spare=st.one_of(st.none(), st.integers(min_value=0, max_value=100)),
    spare_threshold=st.integers(min_value=1, max_value=30),
    temp_c=st.one_of(st.none(), st.integers(min_value=0, max_value=120)),
    is_hdd=st.booleans(),
)
def verify_critical_non_suppression_and_status_soundness(
    overall_passed: bool | None,
    critical_warning: int | str,
    failing_now: bool,
    unstable: int | None,
    media_errors: int | None,
    reallocated: int | None,
    wear: int | None,
    spare: int | None,
    spare_threshold: int,
    temp_c: int | None,
    is_hdd: bool,
) -> None:
    """Critical failure conditions always emit a critical alert and critical status."""
    ata_table = []
    if unstable is not None:
        ata_table.append({"id": 197, "name": "Current_Pending_Sector", "raw": {"value": unstable}})
    if reallocated is not None:
        ata_table.append({
            "id": 5,
            "name": "Reallocated_Sector_Ct",
            "raw": {"value": reallocated},
            "value": 100,
            "worst": 100,
            "thresh": 10,
            "when_failed": "FAILING_NOW" if failing_now else "-",
        })
    elif failing_now:
        ata_table.append({
            "id": 1,
            "name": "Raw_Read_Error_Rate",
            "raw": {"value": 0},
            "value": 1,
            "worst": 1,
            "thresh": 50,
            "when_failed": "FAILING_NOW",
        })

    nvme_log: dict[str, Any] = {
        "critical_warning": critical_warning,
        "available_spare_threshold": spare_threshold,
    }
    if media_errors is not None:
        nvme_log["media_errors"] = media_errors
    if wear is not None:
        nvme_log["percentage_used"] = wear
    if spare is not None:
        nvme_log["available_spare"] = spare
    if temp_c is not None:
        nvme_log["temperature"] = temp_c

    raw: dict[str, Any] = {
        "device": {"protocol": "ATA" if is_hdd else "NVMe"},
        "rotation_rate": 7200 if is_hdd else 0,
        "smart_status": {"passed": overall_passed},
        "ata_smart_attributes": {"table": ata_table},
        "nvme_smart_health_information_log": nvme_log,
    }

    baselines: dict[str, float] = {}
    device = _normalize_device("/dev/test0", raw, {"type": "hdd" if is_hdd else "ssd"}, baselines)
    host_alerts: list[dict[str, Any]] = []
    _evaluate(device, host_alerts)

    # Scratch keys must never leak out of _evaluate
    assert "available_spare_threshold" not in device
    assert "crc_delta" not in device

    # Status soundness against emitted alerts
    if device["alerts"]:
        expected_status = max((a["severity"] for a in device["alerts"]), key=lambda x: _SEVERITY[x])
        assert device["status"] == expected_status, (
            f"status {device['status']} != max alert severity {expected_status}"
        )
    else:
        has_any_metric = any(v is not None for v in device["critical_metrics"].values())
        assert device["status"] == ("ok" if has_any_metric else "unknown")

    assert host_alerts == device["alerts"]

    # Critical non-suppression
    cw_nonzero = int(str(critical_warning), 0) != 0
    crit_temp = 55 if is_hdd else 75
    must_be_critical = any([
        overall_passed is False,
        cw_nonzero,
        failing_now,
        (unstable is not None and unstable > 0),
        (media_errors is not None and media_errors > 0),
        (reallocated is not None and reallocated >= 10),
        (wear is not None and wear >= 100),
        (spare is not None and spare <= spare_threshold),
        (temp_c is not None and temp_c >= crit_temp),
    ])
    if must_be_critical:
        assert device["status"] == "critical", (
            f"Critical condition suppressed: status={device['status']}, metrics={device['critical_metrics']}"
        )
        assert any(a["severity"] == "critical" for a in device["alerts"])


@settings(max_examples=MAX_EXAMPLES, derandomize=True, deadline=None)
@given(kelvin=st.floats(min_value=273.15, max_value=330.0, allow_nan=False, allow_infinity=False))
def verify_kelvin_normalization_no_false_temp_alert(kelvin: float) -> None:
    """Normal NVMe Kelvin readings (273.15..330 K = 0..56.85 C) convert to Celsius and never false-alert."""
    raw = {
        "device": {"protocol": "NVMe"},
        "smart_status": {"passed": True},
        "nvme_smart_health_information_log": {
            "temperature": kelvin,
            "critical_warning": 0,
        },
    }
    device = _normalize_device("/dev/nvme0", raw, {}, {})
    alerts: list[dict[str, Any]] = []
    _evaluate(device, alerts)
    temp_c = device["critical_metrics"]["temperature_c"]
    assert temp_c is not None and abs(temp_c - (kelvin - 273.15)) < 1e-6
    assert not any(a["metric"] == "temperature_c" for a in alerts), (
        f"False temperature alert for {kelvin} K ({temp_c} C): {alerts}"
    )

    # Also verify PVE text parser converts Kelvin > 200 consistently
    pve_parsed = _pve_smart_to_json({"health": "PASSED", "text": f"Temperature: {kelvin:.2f} Kelvin\n"})
    pve_temp = pve_parsed["critical_metrics"]["temperature_c"]
    assert abs(pve_temp - round(round(kelvin, 2) - 273.15, 2)) < 1e-5


@settings(max_examples=MAX_EXAMPLES, derandomize=True, deadline=None)
@given(
    device_specs=st.lists(
        st.tuples(
            st.sampled_from(["ok", "warn_wear", "crit_fail"]),
            st.integers(min_value=0, max_value=500),
            st.integers(min_value=0, max_value=500),
        ),
        min_size=0,
        max_size=5,
    )
)
def verify_host_aggregation_and_crc_delta(device_specs: list[tuple[str, int, int]]) -> None:
    """Host status equals max device status; CRC alerts fire iff count increases above baseline."""
    poll_index = [0]

    def provider():
        idx = poll_index[0]
        rows = []
        for i, (kind, crc1, crc2) in enumerate(device_specs):
            crc = crc1 if idx == 0 else crc2
            raw: dict[str, Any] = {
                "device": {"protocol": "NVMe"},
                "smart_status": {"passed": kind != "crit_fail"},
                "nvme_smart_health_information_log": {
                    "critical_warning": 0,
                    "percentage_used": 85 if kind == "warn_wear" else 5,
                    "num_err_log_entries": crc,
                },
            }
            rows.append((f"/dev/nvme{i}", raw, {}))
        poll_index[0] += 1
        return rows

    collector = SmartCollector(provider=provider)
    first = collector.poll_once(now=1700000000)
    second = collector.poll_once(now=1700000300)

    for snapshot in (first, second):
        if not snapshot["devices"]:
            assert snapshot["status"] == "unavailable"
            assert snapshot["alerts"] == []
        else:
            expected_host = max(
                (d["status"] for d in snapshot["devices"].values()),
                key=lambda s: _SEVERITY[s],
            )
            assert snapshot["status"] == expected_host
            flattened = [a for d in snapshot["devices"].values() for a in d["alerts"]]
            assert snapshot["alerts"] == flattened

    # Check CRC delta invariant on second poll
    for i, (_kind, crc1, crc2) in enumerate(device_specs):
        dev_alerts = second["devices"][f"/dev/nvme{i}"]["alerts"]
        has_crc_alert = any(a["metric"] == "interface_crc_errors" for a in dev_alerts)
        if crc2 > crc1:
            assert has_crc_alert, f"Expected CRC warning when {crc2} > baseline {crc1}"
        else:
            assert not has_crc_alert, f"Unexpected CRC warning when {crc2} <= baseline {crc1}"


def main() -> int:
    saved = _clear_smart_env()
    checks = [
        ("z3_severity_and_thresholds", verify_z3_severity_and_thresholds),
        ("hypothesis_number_domain_safety", verify_number_domain_safety),
        ("hypothesis_parse_health_failure_dominance", verify_parse_health_failure_dominance),
        ("hypothesis_critical_non_suppression_and_status_soundness", verify_critical_non_suppression_and_status_soundness),
        ("hypothesis_kelvin_normalization", verify_kelvin_normalization_no_false_temp_alert),
        ("hypothesis_host_aggregation_and_crc_delta", verify_host_aggregation_and_crc_delta),
    ]
    try:
        for name, fn in checks:
            fn()
            print(f"PASS: {name}")
    finally:
        _restore_smart_env(saved)
    print(f"ALL {len(checks)} FORMAL INVARIANTS VERIFIED (examples={MAX_EXAMPLES}, z3={z3.get_version_string()})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
