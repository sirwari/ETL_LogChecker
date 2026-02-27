import etl_agent


def _sample_metrics():
    return {
        "metadata": {"etl_path": "test.etl"},
        "trace": {"duration_s": 10.0, "event_count": 100},
        "boot": {
            "boot_duration_s": 4.0,
            "explorer_start_s": 3.0,
            "first_user_app_s": 5.0,
            "boot_order": [{"image": "explorer.exe", "start_s": 3.0}],
        },
        "io": {
            "total_ops": 100,
            "total_bytes": 2048,
            "slow_time_s": 1.2,
            "slow_time_pct": 0.12,
            "percentiles_s": {"p95_s": 0.5, "p99_s": 1.0},
        },
        "launch_latency": {"stats": {"p95_s": 1.0}, "top": [{"image": "app.exe"}]},
        "top_processes": {
            "by_slow_time": [{"image": "app.exe", "slow_time_s": 1.0}],
            "by_slow_ops": [{"image": "app.exe", "slow_ops": 3}],
            "by_io_bytes": [{"image": "app.exe", "io_bytes": 1000}],
        },
        "top_files": [{"file": "C:/file", "slow_time_s": 1.0}],
        "process_lifetimes": [{"image": "app.exe", "lifetime_s": 9.0}],
    }


def test_compact_summary_shape():
    summary = etl_agent.compact_metrics_summary(_sample_metrics(), top_n=1)
    assert "trace" in summary
    assert "io" in summary
    assert "boot" in summary
    assert len(summary["top_processes"]["by_slow_time"]) == 1
    assert len(summary["top_files"]) == 1


def test_review_fallback(monkeypatch):
    def _boom(*args, **kwargs):
        raise RuntimeError("ollama down")

    monkeypatch.setattr(etl_agent, "ollama_chat", _boom)
    result = etl_agent.review_with_llm(_sample_metrics())
    assert result["heuristic_fallback"] is True
    assert "insights" in result
    assert "summary" in result["insights"]
