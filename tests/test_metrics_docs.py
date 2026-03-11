import sys

import etl_logchecker
from etl_logchecker import ETLUXAnalyzer


def test_build_metrics_includes_process_and_boot_counts():
    ux = ETLUXAnalyzer("synthetic.etl", bootlog=True)
    ux.start_ts = 0.0
    ux.end_ts = 10.0
    ux.event_count = 100
    ux.pid_start_ts = {10: 0.0, 20: 1.0}
    ux.pid_end_ts = {10: 5.0, 20: 7.0}
    ux.pid_info = {
        10: {"image": "system.exe", "session_id": 0},
        20: {"image": "app.exe", "session_id": 1},
    }
    ux.boot_order = [
        {"pid": 20, "image": "app.exe", "session_id": 1, "start_s": 1.0},
    ]
    ux.total_io_ops = 4
    ux.total_io_bytes = 512
    ux.slow_io_ops = 1
    ux.total_slow_io_time_s = 0.5
    ux.pid_first_io_ts = {20: 1.5}

    metrics = ux._build_metrics()

    assert metrics["trace"]["process_count"] == 2
    assert metrics["trace"]["user_process_count"] == 1
    assert metrics["trace"]["user_process_ratio_pct"] == 0.5
    assert metrics["trace"]["events_per_user_process"] == 100.0
    assert metrics["boot"]["boot_order_count"] == 1
    assert metrics["io"]["slow_ops_per_s"] == 0.1
    assert metrics["io"]["slow_ops_per_user_process"] == 1.0
    assert metrics["io"]["slow_time_avg_ms"] == 500.0
    assert metrics["io"]["slow_time_per_user_process_s"] == 0.5
    assert metrics["io"]["bytes_per_user_process"] == 512.0
    assert metrics["launch_latency"]["stats"]["coverage_pct"] == 1.0


def test_main_prints_gui_quickstart(monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", ["etl_logchecker.py", "--gui-quickstart"])

    assert etl_logchecker.main() == 0
    output = capsys.readouterr().out

    assert "ETL LogChecker GUI Quick Start" in output
    assert "./install.sh" in output
    assert "--gui" in output
