from etl_agent import compare_and_score, compare_many


def _metrics(duration, slow_time, slow_pct, p95, p99, explorer, boot):
    return {
        "trace": {"duration_s": duration},
        "io": {
            "slow_time_s": slow_time,
            "slow_time_pct": slow_pct,
            "percentiles_s": {"p95_s": p95, "p99_s": p99},
        },
        "boot": {"explorer_start_s": explorer, "boot_duration_s": boot},
    }


def test_compare_and_score_classification():
    baseline = _metrics(10.0, 2.0, 0.2, 0.5, 1.0, 3.0, 4.0)
    current = _metrics(8.0, 1.0, 0.1, 0.25, 0.5, 2.0, 3.0)

    result = compare_and_score(current, baseline)
    assert result["improvements"]
    assert not result["regressions"]
    assert result["score"] < 0


def test_compare_and_score_neutral_band():
    baseline = _metrics(10.0, 2.0, 0.2, 0.5, 1.0, 3.0, 4.0)
    current = _metrics(10.2, 2.0, 0.2, 0.5, 1.0, 3.0, 4.0)

    result = compare_and_score(current, baseline)
    assert result["improvements"] == []
    assert result["regressions"] == []


def test_compare_many_ranking():
    baseline = _metrics(10.0, 2.0, 0.2, 0.5, 1.0, 3.0, 4.0)
    current_better = _metrics(8.0, 1.0, 0.1, 0.25, 0.5, 2.0, 3.0)
    current_worse = _metrics(12.0, 3.0, 0.3, 0.6, 1.2, 4.0, 5.0)

    result = compare_many([current_worse, current_better], baseline)
    ranked = result["ranked"]
    assert ranked[0]["score"] < ranked[1]["score"]
