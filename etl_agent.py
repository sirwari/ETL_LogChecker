from __future__ import annotations

import json
import os
import urllib.request
from typing import Any, Iterable

from etl_logchecker import _compare_metrics


NEUTRAL_BAND_PCT = 0.05
SCORE_WEIGHTS = {
    "duration_s": 0.2,
    "slow_io_time_s": 0.2,
    "slow_io_pct": 0.2,
    "io_p95_s": 0.1,
    "io_p99_s": 0.1,
    "explorer_start_s": 0.1,
    "boot_duration_s": 0.1,
}

METRIC_LABELS = {
    "duration_s": "Trace duration",
    "slow_io_time_s": "Slow I/O time",
    "slow_io_pct": "Slow I/O %",
    "io_p95_s": "I/O p95",
    "io_p99_s": "I/O p99",
    "explorer_start_s": "Explorer start",
    "boot_duration_s": "Boot duration",
}


def load_metrics(path_or_dict: Any) -> dict[str, Any]:
    if isinstance(path_or_dict, dict):
        return path_or_dict
    if isinstance(path_or_dict, str):
        with open(path_or_dict, "r", encoding="utf-8") as handle:
            return json.load(handle)
    raise TypeError("metrics input must be a dict or JSON file path")


def compact_metrics_summary(metrics: dict[str, Any], top_n: int = 5) -> dict[str, Any]:
    trace = metrics.get("trace", {}) or {}
    io = metrics.get("io", {}) or {}
    boot = metrics.get("boot", {}) or {}
    launch = metrics.get("launch_latency", {}) or {}
    top_processes = metrics.get("top_processes", {}) or {}
    top_files = metrics.get("top_files", []) or []
    process_lifetimes = metrics.get("process_lifetimes", []) or []

    def _take(items: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
        return list(items)[:top_n]

    return {
        "metadata": metrics.get("metadata", {}) or {},
        "trace": {
            "duration_s": trace.get("duration_s"),
            "event_count": trace.get("event_count"),
        },
        "boot": {
            "boot_duration_s": boot.get("boot_duration_s"),
            "explorer_start_s": boot.get("explorer_start_s"),
            "first_user_app_s": boot.get("first_user_app_s"),
            "boot_order": _take(boot.get("boot_order", []) or []),
        },
        "io": {
            "total_ops": io.get("total_ops"),
            "total_bytes": io.get("total_bytes"),
            "slow_time_s": io.get("slow_time_s"),
            "slow_time_pct": io.get("slow_time_pct"),
            "percentiles_s": io.get("percentiles_s", {}) or {},
        },
        "launch_latency": {
            "stats": launch.get("stats", {}) or {},
            "top": _take(launch.get("top", []) or []),
        },
        "top_processes": {
            "by_slow_time": _take(top_processes.get("by_slow_time", []) or []),
            "by_slow_ops": _take(top_processes.get("by_slow_ops", []) or []),
            "by_io_bytes": _take(top_processes.get("by_io_bytes", []) or []),
        },
        "top_files": _take(top_files),
        "process_lifetimes": _take(process_lifetimes),
    }


def _classify_delta(metric_key: str, entry: dict[str, Any]) -> dict[str, Any]:
    pct = entry.get("pct")
    if pct is None:
        status = "neutral"
    elif pct <= -NEUTRAL_BAND_PCT:
        status = "improvement"
    elif pct >= NEUTRAL_BAND_PCT:
        status = "regression"
    else:
        status = "neutral"
    return {
        "metric": metric_key,
        "label": METRIC_LABELS.get(metric_key, metric_key),
        "current": entry.get("current"),
        "baseline": entry.get("baseline"),
        "delta": entry.get("delta"),
        "pct": pct,
        "status": status,
    }


def compare_and_score(
    current: dict[str, Any] | str,
    baseline: dict[str, Any] | str,
) -> dict[str, Any]:
    current_metrics = load_metrics(current)
    baseline_metrics = load_metrics(baseline)
    deltas = _compare_metrics(current_metrics, baseline_metrics)

    improvements: list[dict[str, Any]] = []
    regressions: list[dict[str, Any]] = []
    used_weight = 0.0
    score = 0.0

    for metric_key, entry in deltas.items():
        classified = _classify_delta(metric_key, entry)
        if classified["status"] == "improvement":
            improvements.append(classified)
        elif classified["status"] == "regression":
            regressions.append(classified)

        pct = classified.get("pct")
        weight = SCORE_WEIGHTS.get(metric_key)
        if weight is None or pct is None:
            continue
        used_weight += weight
        score += weight * pct

    score_normalized = score / used_weight if used_weight else 0.0

    return {
        "deltas": deltas,
        "improvements": improvements,
        "regressions": regressions,
        "score": score_normalized,
    }


def compare_many(
    currents: list[dict[str, Any] | str],
    baseline: dict[str, Any] | str,
) -> dict[str, Any]:
    comparisons = []
    for idx, current in enumerate(currents):
        label = f"run_{idx + 1}"
        if isinstance(current, str):
            label = os.path.basename(current) or label
        result = compare_and_score(current, baseline)
        comparisons.append(
            {
                "label": label,
                "deltas": result["deltas"],
                "improvements": result["improvements"],
                "regressions": result["regressions"],
                "score": result["score"],
            }
        )

    ranked = sorted(comparisons, key=lambda r: r["score"])
    return {"comparisons": comparisons, "ranked": ranked}


def ollama_chat(
    messages: list[dict[str, str]],
    host: str,
    model: str,
    temperature: float,
    max_tokens: int,
    timeout_s: float = 20.0,
) -> dict[str, Any]:
    payload = {
        "model": model,
        "messages": messages,
        "stream": False,
        "options": {
            "temperature": temperature,
            "num_predict": max_tokens,
        },
    }
    data = json.dumps(payload).encode("utf-8")
    url = host.rstrip("/") + "/api/chat"
    request = urllib.request.Request(
        url,
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=timeout_s) as response:
        return json.load(response)


def _fallback_insights(
    summary: dict[str, Any],
    comparison: dict[str, Any] | None,
) -> dict[str, Any]:
    trace = summary.get("trace", {}) or {}
    io = summary.get("io", {}) or {}
    boot = summary.get("boot", {}) or {}
    observations = [
        f"Trace duration: {trace.get('duration_s')}",
        f"Slow I/O time: {io.get('slow_time_s')} ({io.get('slow_time_pct')})",
        f"Boot duration: {boot.get('boot_duration_s')}",
    ]
    improvements = []
    regressions = []
    if comparison:
        improvements = comparison.get("improvements", [])
        regressions = comparison.get("regressions", [])
    recommendations = []
    if regressions:
        recommendations.append("Investigate top regressions and compare offenders in slow I/O and launch latency.")
    if summary.get("top_files"):
        recommendations.append("Review top files by slow I/O time for caching or prefetch opportunities.")
    if not recommendations:
        recommendations.append("Review top slow I/O offenders and app launch latency outliers.")

    return {
        "summary": "Heuristic fallback summary based on metrics.",
        "regressions": regressions,
        "improvements": improvements,
        "observations": observations,
        "recommendations": recommendations,
        "questions": [],
    }


def review_with_llm(
    current: dict[str, Any] | str,
    baseline: dict[str, Any] | str | None = None,
    ollama_host: str | None = None,
    model: str | None = None,
    temperature: float = 0.2,
    max_tokens: int = 800,
    focus: str | None = None,
    return_raw: bool = False,
) -> dict[str, Any]:
    current_metrics = load_metrics(current)
    summary = compact_metrics_summary(current_metrics)
    comparison_result = None
    if baseline is not None:
        comparison_result = compare_and_score(current_metrics, baseline)

    host = ollama_host or os.environ.get("OLLAMA_HOST", "http://localhost:11434")
    model_name = model or os.environ.get("OLLAMA_MODEL", "gptoss20b")

    system_prompt = (
        "You are a performance analysis assistant. "
        "Return ONLY strict JSON with keys: summary, regressions, improvements, "
        "observations, recommendations, questions."
    )
    user_payload = {
        "summary": summary,
        "comparison": comparison_result,
        "focus": focus,
    }

    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": json.dumps(user_payload)},
    ]

    raw_response = None
    try:
        raw_response = ollama_chat(
            messages=messages,
            host=host,
            model=model_name,
            temperature=temperature,
            max_tokens=max_tokens,
        )
        content = raw_response.get("message", {}).get("content", "")
        parsed = json.loads(content)
        return {
            "insights": parsed,
            "comparison": comparison_result,
            "raw_llm": content if return_raw else None,
            "heuristic_fallback": False,
        }
    except Exception:
        fallback = _fallback_insights(summary, comparison_result)
        return {
            "insights": fallback,
            "comparison": comparison_result,
            "raw_llm": raw_response if return_raw else None,
            "heuristic_fallback": True,
        }
