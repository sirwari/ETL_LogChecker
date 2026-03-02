import os
import sys

import etl_logchecker


def test_suggest_output_paths_uses_etl_stem():
    paths = etl_logchecker._suggest_output_paths("/tmp/bootLog.etl")

    assert paths["report_path"] == os.path.join("/tmp", "bootLog_report.html")
    assert paths["metrics_output_path"] == os.path.join("/tmp", "bootLog_metrics.json")
    assert paths["timeline_output_path"] == os.path.join("/tmp", "bootLog_timeline.json")
    assert paths["plot_dir"] == os.path.join("/tmp", "bootLog_plots")


def test_build_analysis_summary_includes_artifacts_and_comparison():
    result = {
        "metrics": {
            "trace": {"duration_s": 10.0, "event_count": 42},
            "io": {
                "slow_time_s": 2.0,
                "slow_time_pct": 0.2,
                "percentiles_s": {"p95_s": 0.5, "p99_s": 1.0},
            },
            "boot": {"boot_duration_s": 4.0},
        },
        "metrics_path": "current.json",
        "baseline_metrics_path": "baseline.json",
        "timeline_path": "timeline.json",
        "report_path": "report.html",
        "plot_paths": {"network_throughput": "plots/network.png"},
        "warnings": ["plot warning"],
        "comparison": {
            "duration_s": {
                "current": 10.0,
                "baseline": 8.0,
                "delta": 2.0,
                "pct": 0.25,
            }
        },
    }

    summary = etl_logchecker._build_analysis_summary(
        result,
        etl_path="trace.etl",
        compare_source="baseline.json",
    )

    assert "ETL file: trace.etl" in summary
    assert "Trace duration: 10.00 s" in summary
    assert "Metrics JSON: current.json" in summary
    assert "Plot (network_throughput): plots/network.png" in summary
    assert "Warnings:" in summary
    assert "Comparison deltas:" in summary


def test_main_routes_gui_mode(monkeypatch):
    called = {}

    def _fake_launch(path):
        called["path"] = path
        return 17

    monkeypatch.setattr(etl_logchecker, "launch_standalone_gui", _fake_launch)
    monkeypatch.setattr(
        sys,
        "argv",
        ["etl_logchecker.py", "--gui", "bootLog.etl"],
    )

    assert etl_logchecker.main() == 17
    assert called["path"] == "bootLog.etl"
