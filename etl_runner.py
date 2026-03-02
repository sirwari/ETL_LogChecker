from __future__ import annotations

import json
import os
from typing import Any

from etl_agent import compare_and_score, compare_many, load_metrics, review_with_llm
from etl_logchecker import (
    ETLUXAnalyzer,
    _build_timeline_rows,
    _collect_network_trends,
    _compare_metrics,
    _derive_baseline_metrics_path,
    _parse_time_scale_arg,
    _read_perf_counter_scale,
    _render_report,
    _write_network_throughput_plot,
    _write_timeline_output,
)


def _resolve_time_scale(etl_path: str, time_scale: str | None) -> tuple[float | None, str]:
    override_scale = _parse_time_scale_arg(time_scale or "auto")
    if override_scale is not None:
        return override_scale, "override"
    perf_scale = _read_perf_counter_scale(etl_path)
    if perf_scale is not None:
        return perf_scale, "perf_freq"
    return None, "auto"


def _write_json(path: str, data: dict[str, Any]) -> None:
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(data, handle, indent=2)


def analyze_etl(
    etl_path: str,
    report_path: str | None = None,
    metrics_output_path: str | None = None,
    compare_metrics_path: str | None = None,
    compare_etl_path: str | None = None,
    timeline_output_path: str | None = None,
    timeline_format: str = "json",
    plot_dir: str | None = None,
    slow_io_ms: int = 50,
    top_n: int = 10,
    time_scale: str | None = "auto",
    bootlog: bool = False,
    boot_window_s: float = 300.0,
    return_metrics: bool = True,
    return_baseline_metrics: bool = False,
) -> dict[str, Any]:
    if compare_metrics_path and compare_etl_path:
        raise ValueError("Provide either compare_metrics_path or compare_etl_path, not both.")
    if not os.path.isfile(etl_path):
        raise ValueError(f"ETL file not found: {etl_path}")
    if compare_etl_path and not os.path.isfile(compare_etl_path):
        raise ValueError(f"Baseline ETL file not found: {compare_etl_path}")
    if compare_metrics_path and not os.path.isfile(compare_metrics_path):
        raise ValueError(f"Baseline metrics file not found: {compare_metrics_path}")

    timestamp_scale, time_scale_source = _resolve_time_scale(etl_path, time_scale)

    ux = ETLUXAnalyzer(
        etl_path,
        slow_io_ms=slow_io_ms,
        top_n=top_n,
        debug=False,
        progress_every=0,
        timestamp_scale=timestamp_scale,
        time_scale_source=time_scale_source,
        bootlog=bootlog,
        boot_window_s=boot_window_s,
    )
    metrics = ux.analyze()

    if metrics_output_path:
        _write_json(metrics_output_path, metrics)

    baseline_metrics = None
    baseline_metrics_path = None
    if compare_metrics_path:
        baseline_metrics = load_metrics(compare_metrics_path)
    elif compare_etl_path:
        baseline_ux = ETLUXAnalyzer(
            compare_etl_path,
            slow_io_ms=slow_io_ms,
            top_n=top_n,
            debug=False,
            progress_every=0,
            timestamp_scale=timestamp_scale,
            time_scale_source=time_scale_source,
            bootlog=bootlog,
            boot_window_s=boot_window_s,
        )
        baseline_metrics = baseline_ux.analyze()
        if metrics_output_path or report_path:
            baseline_metrics_path = _derive_baseline_metrics_path(
                metrics_output_path, report_path
            )
            _write_json(baseline_metrics_path, baseline_metrics)

    comparison = _compare_metrics(metrics, baseline_metrics) if baseline_metrics else None

    timeline_path = None
    if timeline_output_path:
        trace_duration = metrics.get("trace", {}).get("duration_s") or 0.0
        rows = _build_timeline_rows(
            ux.pid_start_ts,
            ux.pid_end_ts,
            ux.pid_info,
            ux.pid_first_io_ts,
            ux.pid_first_image_ts,
            trace_duration,
        )
        _write_timeline_output(rows, timeline_output_path, timeline_format)
        timeline_path = timeline_output_path

    warnings: list[str] = []
    plot_paths: dict[str, str] = {}
    if plot_dir:
        trends = _collect_network_trends(
            etl_path,
            1.0,
            timestamp_scale,
            False,
            None,
            True,
            True,
            False,
        )
        plot_path = _write_network_throughput_plot(trends, plot_dir)
        if plot_path:
            if report_path:
                report_dir = os.path.dirname(report_path) or "."
                plot_paths["network_throughput"] = os.path.relpath(
                    plot_path, report_dir
                )
            else:
                plot_paths["network_throughput"] = plot_path
        else:
            warnings.append("Plot generation skipped (missing matplotlib or no data).")

    if report_path:
        report_html = _render_report(metrics, baseline_metrics, plot_paths)
        with open(report_path, "w", encoding="utf-8") as handle:
            handle.write(report_html)

    response: dict[str, Any] = {
        "comparison": comparison,
        "metrics_path": metrics_output_path,
        "baseline_metrics_path": baseline_metrics_path,
        "report_path": report_path,
        "timeline_path": timeline_path,
        "plot_paths": plot_paths,
        "warnings": warnings,
    }

    if return_metrics:
        response["metrics"] = metrics
    if return_baseline_metrics:
        response["baseline_metrics"] = baseline_metrics

    return response


def compare_metrics(
    current: dict[str, Any] | str | list[dict[str, Any] | str],
    baseline: dict[str, Any] | str,
) -> dict[str, Any]:
    if isinstance(current, list):
        return compare_many(current, baseline)
    result = compare_and_score(current, baseline)
    label = os.path.basename(current) if isinstance(current, str) else "current"
    return {
        "comparisons": [
            {
                "label": label,
                "deltas": result["deltas"],
                "improvements": result["improvements"],
                "regressions": result["regressions"],
                "score": result["score"],
            }
        ],
        "ranked": [
            {
                "label": label,
                "deltas": result["deltas"],
                "improvements": result["improvements"],
                "regressions": result["regressions"],
                "score": result["score"],
            }
        ],
    }


def review_metrics(
    current: dict[str, Any] | str,
    baseline: dict[str, Any] | str | None = None,
    ollama_host: str | None = None,
    model: str | None = None,
    temperature: float = 0.2,
    max_tokens: int = 800,
    focus: str | None = None,
    return_raw: bool = False,
) -> dict[str, Any]:
    return review_with_llm(
        current=current,
        baseline=baseline,
        ollama_host=ollama_host,
        model=model,
        temperature=temperature,
        max_tokens=max_tokens,
        focus=focus,
        return_raw=return_raw,
    )
