import os

import etl_logchecker


def test_run_analysis_job_reports_stage_progress(monkeypatch, tmp_path):
    current_etl = tmp_path / "current.etl"
    baseline_etl = tmp_path / "baseline.etl"
    current_etl.write_bytes(b"current")
    baseline_etl.write_bytes(b"baseline")

    metrics_path = tmp_path / "current_metrics.json"
    report_path = tmp_path / "current_report.html"
    analyzed_paths: list[str] = []

    class _FakeAnalyzer:
        def __init__(self, etl_path, **_kwargs):
            self.etl_path = etl_path
            self.pid_start_ts = {}
            self.pid_end_ts = {}
            self.pid_info = {}
            self.pid_first_io_ts = {}
            self.pid_first_image_ts = {}

        def analyze(self):
            analyzed_paths.append(self.etl_path)
            return {"trace": {"duration_s": 1.0, "event_count": 1}}

    monkeypatch.setattr(etl_logchecker, "ETLUXAnalyzer", _FakeAnalyzer)
    monkeypatch.setattr(
        etl_logchecker,
        "_resolve_time_scale",
        lambda _etl_path, _time_scale: (None, "auto"),
    )
    monkeypatch.setattr(
        etl_logchecker,
        "_compare_metrics",
        lambda _metrics, _baseline: {"duration_s": {"delta": 0.0, "pct": 0.0}},
    )
    monkeypatch.setattr(
        etl_logchecker,
        "_render_report",
        lambda _metrics, _baseline, _plots: "<html>report</html>",
    )

    progress_updates: list[str] = []
    result = etl_logchecker.run_analysis_job(
        etl_path=str(current_etl),
        report_path=str(report_path),
        metrics_output_path=str(metrics_path),
        compare_etl_path=str(baseline_etl),
        plot_dir=None,
        timeline_output_path=None,
        return_metrics=False,
        return_baseline_metrics=False,
        progress_callback=progress_updates.append,
    )

    assert progress_updates == [
        "Analyzing primary ETL trace...",
        "Primary ETL analysis complete.",
        "Writing primary metrics JSON...",
        "Analyzing baseline ETL trace...",
        "Baseline ETL analysis complete.",
        "Writing baseline metrics JSON...",
        "Rendering HTML report...",
        "Finalizing results...",
    ]
    assert analyzed_paths == [str(current_etl), str(baseline_etl)]
    assert os.path.isfile(metrics_path)
    assert os.path.isfile(tmp_path / "current_metrics_baseline.json")
    assert os.path.isfile(report_path)
    assert result["metrics_path"] == str(metrics_path)
    assert result["baseline_metrics_path"] == str(tmp_path / "current_metrics_baseline.json")


def test_run_analysis_job_reports_incremental_updates(monkeypatch, tmp_path):
    current_etl = tmp_path / "current.etl"
    baseline_etl = tmp_path / "baseline.etl"
    current_etl.write_bytes(b"current")
    baseline_etl.write_bytes(b"baseline")

    metrics_path = tmp_path / "current_metrics.json"
    report_path = tmp_path / "current_report.html"

    class _FakeAnalyzer:
        def __init__(self, etl_path, **_kwargs):
            self.etl_path = etl_path
            self.pid_start_ts = {}
            self.pid_end_ts = {}
            self.pid_info = {}
            self.pid_first_io_ts = {}
            self.pid_first_image_ts = {}

        def analyze(self):
            if self.etl_path == str(current_etl):
                return {"trace": {"duration_s": 1.0, "event_count": 1}}
            return {"trace": {"duration_s": 0.5, "event_count": 1}}

    monkeypatch.setattr(etl_logchecker, "ETLUXAnalyzer", _FakeAnalyzer)
    monkeypatch.setattr(
        etl_logchecker,
        "_resolve_time_scale",
        lambda _etl_path, _time_scale: (None, "auto"),
    )
    monkeypatch.setattr(
        etl_logchecker,
        "_compare_metrics",
        lambda _metrics, _baseline: {"duration_s": {"delta": 0.5, "pct": 1.0}},
    )
    monkeypatch.setattr(
        etl_logchecker,
        "_render_report",
        lambda _metrics, _baseline, _plots: "<html>report</html>",
    )

    progress_events: list[dict[str, object]] = []
    etl_logchecker.run_analysis_job(
        etl_path=str(current_etl),
        report_path=str(report_path),
        metrics_output_path=str(metrics_path),
        compare_etl_path=str(baseline_etl),
        plot_dir=None,
        timeline_output_path=None,
        return_metrics=False,
        return_baseline_metrics=False,
        progress_event_callback=progress_events.append,
    )

    assert any(
        event["status_text"] == "Analyzing primary ETL trace..."
        and event["updates"] == {}
        for event in progress_events
    )
    assert any(
        event["status_text"] == "Primary ETL analysis complete."
        and event["updates"] == {"metrics": {"trace": {"duration_s": 1.0, "event_count": 1}}}
        for event in progress_events
    )
    assert any(
        event["status_text"] == "Writing primary metrics JSON..."
        and event["updates"] == {"metrics_path": str(metrics_path)}
        for event in progress_events
    )
    assert any(
        event["status_text"] == "Baseline ETL analysis complete."
        and event["updates"] == {
            "baseline_metrics": {"trace": {"duration_s": 0.5, "event_count": 1}},
            "comparison": {"duration_s": {"delta": 0.5, "pct": 1.0}},
        }
        for event in progress_events
    )
    assert any(
        event["status_text"] == "Rendering HTML report..."
        and event["updates"] == {
            "report_html": "<html>report</html>",
            "report_path": str(report_path),
        }
        for event in progress_events
    )


def test_run_analysis_job_plot_collection_failures_do_not_abort(monkeypatch, tmp_path):
    current_etl = tmp_path / "current.etl"
    current_etl.write_bytes(b"current")

    output_dir = tmp_path / "bundle" / "current_20260305_0900"
    metrics_path = output_dir / "current_metrics.json"
    report_path = output_dir / "current_report.html"
    timeline_path = output_dir / "current_timeline.json"
    plot_dir = output_dir / "current_plots"

    class _FakeAnalyzer:
        def __init__(self, _etl_path, **_kwargs):
            self.pid_start_ts = {}
            self.pid_end_ts = {}
            self.pid_info = {}
            self.pid_first_io_ts = {}
            self.pid_first_image_ts = {}

        def analyze(self):
            return {
                "trace": {"duration_s": 1.0, "event_count": 1},
                "io": {"percentiles_s": {}, "histogram": {}},
                "launch_latency": {"stats": {}},
                "top_processes": {},
                "top_files": [],
                "boot": {},
            }

    monkeypatch.setattr(etl_logchecker, "ETLUXAnalyzer", _FakeAnalyzer)
    monkeypatch.setattr(
        etl_logchecker,
        "_resolve_time_scale",
        lambda _etl_path, _time_scale: (None, "auto"),
    )
    monkeypatch.setattr(
        etl_logchecker,
        "_collect_network_trends",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("trend parser failed")),
    )
    monkeypatch.setattr(
        etl_logchecker,
        "_render_report",
        lambda _metrics, _baseline, _plots: "<html>report</html>",
    )

    result = etl_logchecker.run_analysis_job(
        etl_path=str(current_etl),
        report_path=str(report_path),
        metrics_output_path=str(metrics_path),
        timeline_output_path=str(timeline_path),
        plot_dir=str(plot_dir),
        return_metrics=False,
        return_baseline_metrics=False,
    )

    assert metrics_path.is_file()
    assert report_path.is_file()
    assert timeline_path.is_file()
    assert plot_dir.is_dir()
    assert result["report_path"] == str(report_path)
    assert any("network trend collection failed" in warning for warning in result["warnings"])
