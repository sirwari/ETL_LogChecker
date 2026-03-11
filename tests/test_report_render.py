from etl_logchecker import _format_review_diagnose, _render_report


def test_render_report_shows_added_metrics():
    metrics = {
        "metadata": {
            "generated_at": "2026-03-03T12:00:00Z",
            "etl_path": "sample.etl",
        },
        "trace": {
            "duration_s": 12.0,
            "event_count": 120,
            "events_per_s": 10.0,
            "events_per_process": 15.0,
            "events_per_user_process": 20.0,
            "process_count": 8,
            "user_process_count": 6,
            "user_process_ratio_pct": 0.75,
        },
        "boot": {
            "boot_duration_s": 5.0,
            "explorer_start_s": 4.0,
            "boot_order_count": 2,
            "boot_order": [],
        },
        "io": {
            "total_ops": 50,
            "total_bytes": 4096,
            "avg_bytes_per_op": 81.92,
            "bytes_per_user_process": 682.66,
            "throughput_bytes_per_s": 341.33,
            "slow_ops": 5,
            "slow_ops_pct": 0.1,
            "slow_time_s": 1.5,
            "slow_time_pct": 0.125,
            "slow_ops_per_s": 0.42,
            "slow_ops_per_user_process": 0.83,
            "slow_time_avg_ms": 300.0,
            "slow_time_per_user_process_s": 0.25,
            "percentiles_s": {"p50_s": 0.1, "p95_s": 0.5, "p99_s": 1.0},
            "histogram": {"<= 1 ms": 10},
        },
        "launch_latency": {
            "stats": {"avg_s": 0.6, "p50_s": 0.5, "p95_s": 0.9, "coverage_pct": 0.66},
            "top": [{"image": "app.exe", "pid": 1, "session_id": 1, "startup_latency_s": 0.9, "first_signal": "disk_io"}],
        },
        "top_processes": {
            "by_slow_time": [{"image": "app.exe", "pid": 1, "slow_time_s": 1.0, "slow_ops": 2}],
            "by_slow_ops": [{"image": "app.exe", "pid": 1, "slow_ops": 2, "slow_time_s": 1.0}],
            "by_io_bytes": [{"image": "app.exe", "pid": 1, "io_bytes": 2048, "io_ops": 3}],
        },
        "top_files": [{"file": "C:/trace/file.bin", "slow_time_s": 0.5, "slow_ops": 1}],
        "process_lifetimes": [{"image": "app.exe", "pid": 1, "lifetime_s": 10.0}],
    }

    html = _render_report(metrics)

    assert "Events / s" in html
    assert "Events / process" in html
    assert "Events / user process" in html
    assert "Processes" in html
    assert "User processes" in html
    assert "User process ratio" in html
    assert "Slow ops %" in html
    assert "Slow ops / s" in html
    assert "Slow ops / user process" in html
    assert "Avg slow I/O (ms)" in html
    assert "Slow I/O s / user process" in html
    assert "I/O p99" in html
    assert "I/O throughput" in html
    assert "Launch avg:" in html
    assert "Launch coverage:" in html
    assert "Avg I/O bytes / op:" in html
    assert "I/O bytes / user process:" in html
    assert "Boot order entries: 2" in html


def test_format_review_diagnose_includes_backend_details():
    result = {
        "backend": {
            "provider": "ollama",
            "host": "http://localhost:11434",
            "model": "ministral:latest",
            "used_ollama": False,
            "status": "fallback",
            "error": "connection refused",
            "attempted_models": ["ministral:latest", "ministral-3:latest"],
            "local_models": ["mistral"],
            "endpoint": "/api/generate",
            "attempted_endpoints": ["/api/chat", "/api/generate"],
            "request_timeout_s": 90.0,
            "transport": "http",
            "cli_fallback_enabled": True,
            "cli_error": "\x1b[?25lollama CLI not found on PATH.\x1b[?25h",
        },
        "insights": {
            "summary": "Fallback summary.",
            "observations": ["Slow I/O stable."],
            "regressions": [{"label": "Launch p95", "pct": 0.12}],
            "improvements": [],
            "recommendations": ["Check the Ollama service."],
            "questions": [],
        },
    }

    text = _format_review_diagnose(result)

    assert "Agentic Diagnose (Ollama)" in text
    assert "Status: Heuristic fallback" in text
    assert "Endpoint: /api/generate" in text
    assert "Transport: http" in text
    assert "Attempted endpoints: /api/chat, /api/generate" in text
    assert "Attempted models: ministral:latest, ministral-3:latest" in text
    assert "Local models: mistral" in text
    assert "CLI fallback enabled: yes" in text
    assert "CLI fallback error: ollama CLI not found on PATH." in text
    assert "Timeout: 90.0s" in text
    assert "Fallback reason: connection refused" in text
    assert "- Launch p95 (12.00%)" in text
