from __future__ import annotations

import os
from typing import Any

from etl_agent import compare_and_score, compare_many, review_with_llm
from etl_logchecker import run_analysis_job


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
    return run_analysis_job(
        etl_path,
        report_path=report_path,
        metrics_output_path=metrics_output_path,
        compare_metrics_path=compare_metrics_path,
        compare_etl_path=compare_etl_path,
        timeline_output_path=timeline_output_path,
        timeline_format=timeline_format,
        plot_dir=plot_dir,
        slow_io_ms=slow_io_ms,
        top_n=top_n,
        time_scale=time_scale,
        bootlog=bootlog,
        boot_window_s=boot_window_s,
        debug=False,
        progress_every=0,
        return_metrics=return_metrics,
        return_baseline_metrics=return_baseline_metrics,
    )


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
