import os
import queue
import re
import sys

import etl_logchecker


def test_suggest_output_paths_uses_etl_stem():
    paths = etl_logchecker._suggest_output_paths("/tmp/bootLog.etl")

    run_dir = os.path.dirname(paths["report_path"])
    assert os.path.dirname(run_dir) == "/tmp"
    assert re.fullmatch(r"bootLog_\d{8}_\d{4}", os.path.basename(run_dir))
    assert paths["report_path"] == os.path.join(run_dir, "bootLog_report.html")
    assert paths["metrics_output_path"] == os.path.join(run_dir, "bootLog_metrics.json")
    assert paths["timeline_output_path"] == os.path.join(run_dir, "bootLog_timeline.json")
    assert paths["plot_dir"] == os.path.join(run_dir, "bootLog_plots")


def test_resolve_analysis_output_paths_auto_uses_suggested_names():
    paths = etl_logchecker._resolve_analysis_output_paths(
        "/tmp/bootLog.etl",
        auto_generate=True,
    )

    report_path = str(paths["report_path"])
    run_dir = os.path.dirname(report_path)
    assert os.path.dirname(run_dir) == "/tmp"
    assert re.fullmatch(r"bootLog_\d{8}_\d{4}", os.path.basename(run_dir))
    assert paths["report_path"] == os.path.join(run_dir, "bootLog_report.html")
    assert paths["metrics_output_path"] == os.path.join(run_dir, "bootLog_metrics.json")
    assert paths["timeline_output_path"] == os.path.join(run_dir, "bootLog_timeline.json")
    assert paths["plot_dir"] == os.path.join(run_dir, "bootLog_plots")


def test_resolve_analysis_output_paths_manual_preserves_selected_values():
    paths = etl_logchecker._resolve_analysis_output_paths(
        "/tmp/bootLog.etl",
        auto_generate=False,
        report_path="manual_report.html",
        metrics_output_path=None,
        timeline_output_path="manual_timeline.json",
        plot_dir="manual_plots",
    )

    assert paths["report_path"] == "manual_report.html"
    assert paths["metrics_output_path"] is None
    assert paths["timeline_output_path"] == "manual_timeline.json"
    assert paths["plot_dir"] == "manual_plots"


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


def test_build_analysis_summary_includes_stage_details():
    summary = etl_logchecker._build_analysis_summary(
        {"metrics": {}},
        etl_path="trace.etl",
        status_text="Analyzing baseline ETL trace...",
        in_progress=True,
    )

    assert "Stage: Analyzing baseline ETL trace..." in summary
    assert "Run state: In progress" in summary


def test_merge_analysis_progress_result_replaces_mutable_fields():
    merged = etl_logchecker._merge_analysis_progress_result(
        {
            "warnings": ["old warning"],
            "plot_paths": {"old": "old.png"},
            "metrics": {"trace": {"duration_s": 1.0}},
        },
        {
            "warnings": ["new warning"],
            "plot_paths": {"network_throughput": "new.png"},
            "comparison": {"duration_s": {"delta": 0.5}},
        },
    )

    assert merged["warnings"] == ["new warning"]
    assert merged["plot_paths"] == {"network_throughput": "new.png"}
    assert merged["metrics"] == {"trace": {"duration_s": 1.0}}
    assert merged["comparison"] == {"duration_s": {"delta": 0.5}}


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


def test_run_gui_background_task_reports_success():
    task_queue: queue.Queue[tuple[str, object]] = queue.Queue()

    etl_logchecker._run_gui_background_task(
        task_queue,
        lambda: {"ok": True, "count": 1},
    )

    result_type, payload = task_queue.get_nowait()

    assert result_type == "success"
    assert payload == {"ok": True, "count": 1}


def test_run_gui_background_task_reports_errors():
    task_queue: queue.Queue[tuple[str, object]] = queue.Queue()

    def _raise_error():
        raise RuntimeError("worker failed")

    etl_logchecker._run_gui_background_task(task_queue, _raise_error)

    result_type, payload = task_queue.get_nowait()

    assert result_type == "error"
    assert isinstance(payload, RuntimeError)
    assert str(payload) == "worker failed"
