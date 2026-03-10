from etl_logchecker import _compare_metrics


def test_compare_metrics_deltas():
    current = {
        "trace": {"duration_s": 10.0},
        "io": {
            "slow_time_s": 2.0,
            "slow_time_pct": 0.2,
            "percentiles_s": {"p95_s": 0.5, "p99_s": 1.0},
        },
        "boot": {"explorer_start_s": 3.0, "boot_duration_s": 4.0},
    }
    baseline = {
        "trace": {"duration_s": 8.0},
        "io": {
            "slow_time_s": 1.0,
            "slow_time_pct": 0.1,
            "percentiles_s": {"p95_s": 0.25, "p99_s": 0.5},
        },
        "boot": {"explorer_start_s": 2.0, "boot_duration_s": 3.0},
    }

    deltas = _compare_metrics(current, baseline)
    assert deltas["duration_s"]["delta"] == 2.0
    assert deltas["slow_io_time_s"]["delta"] == 1.0
    assert deltas["slow_io_pct"]["delta"] == 0.1
    assert deltas["explorer_start_s"]["delta"] == 1.0
    assert deltas["boot_duration_s"]["delta"] == 1.0
