from __future__ import annotations

import json
import math
import os
import re
import urllib.request
from typing import Any, Iterable

from etl_logchecker import _compare_metrics


NEUTRAL_BAND_PCT = 0.05
SCORE_WEIGHTS = {
    "duration_s": 0.15,
    "slow_io_time_s": 0.2,
    "slow_io_pct": 0.15,
    "slow_io_ops_pct": 0.1,
    "io_p95_s": 0.1,
    "io_p99_s": 0.05,
    "launch_p95_s": 0.1,
    "explorer_start_s": 0.1,
    "boot_duration_s": 0.05,
}

METRIC_LABELS = {
    "duration_s": "Trace duration",
    "slow_io_time_s": "Slow I/O time",
    "slow_io_pct": "Slow I/O %",
    "slow_io_ops_pct": "Slow I/O ops %",
    "io_p95_s": "I/O p95",
    "io_p99_s": "I/O p99",
    "launch_p95_s": "Launch p95",
    "explorer_start_s": "Explorer start",
    "boot_duration_s": "Boot duration",
}

DEFAULT_OLLAMA_HOST = "http://localhost:11434"
DEFAULT_OLLAMA_MODEL = "ministral:latest"
DEFAULT_OLLAMA_TIMEOUT_S = 90.0
_INSIGHT_LIST_KEYS = (
    "regressions",
    "improvements",
    "observations",
    "recommendations",
    "questions",
)


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
            "events_per_s": trace.get("events_per_s"),
            "process_count": trace.get("process_count"),
            "user_process_count": trace.get("user_process_count"),
        },
        "boot": {
            "boot_duration_s": boot.get("boot_duration_s"),
            "explorer_start_s": boot.get("explorer_start_s"),
            "first_user_app_s": boot.get("first_user_app_s"),
            "boot_order_count": boot.get("boot_order_count"),
            "boot_order": _take(boot.get("boot_order", []) or []),
        },
        "io": {
            "total_ops": io.get("total_ops"),
            "total_bytes": io.get("total_bytes"),
            "avg_bytes_per_op": io.get("avg_bytes_per_op"),
            "slow_ops": io.get("slow_ops"),
            "slow_time_s": io.get("slow_time_s"),
            "slow_time_pct": io.get("slow_time_pct"),
            "slow_ops_pct": io.get("slow_ops_pct"),
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


def _resolve_timeout_s(timeout_s: float | None) -> float:
    if timeout_s is not None:
        try:
            value = float(timeout_s)
        except Exception:
            value = DEFAULT_OLLAMA_TIMEOUT_S
        if math.isfinite(value) and value > 0:
            return value
        return DEFAULT_OLLAMA_TIMEOUT_S

    for env_name in ("OLLAMA_TIMEOUT_S", "OLLAMA_TIMEOUT"):
        raw_value = os.environ.get(env_name)
        if not raw_value:
            continue
        try:
            value = float(raw_value)
        except Exception:
            continue
        if math.isfinite(value) and value > 0:
            return value
    return DEFAULT_OLLAMA_TIMEOUT_S


def _build_model_candidates(model_name: str) -> list[str]:
    name = model_name.strip()
    if not name:
        return [DEFAULT_OLLAMA_MODEL]

    candidates: list[str] = []

    def _add(candidate: str) -> None:
        text = candidate.strip()
        if text and text not in candidates:
            candidates.append(text)

    _add(name)
    if ":" not in name and "/" not in name:
        _add(f"{name}:latest")

    lower_name = name.lower()
    if lower_name == "ministral":
        _add("ministral:latest")
        _add("ministral-3:latest")
    elif lower_name.startswith("ministral:"):
        _add("ministral")
        _add("ministral-3:latest")
    elif lower_name.startswith("ministral-3"):
        _add("ministral:latest")
        _add("ministral")

    return candidates


def _is_model_not_found_error(error_text: str) -> bool:
    text = str(error_text).lower()
    if "model" not in text:
        return False
    return (
        "not found" in text
        or "pull" in text
        or "does not exist" in text
    )


def _parse_ollama_content(content: Any) -> tuple[dict[str, Any], str]:
    if isinstance(content, dict):
        return content, "dict"
    if not isinstance(content, str):
        raise ValueError("Ollama response content is not a JSON object.")

    text = content.strip()
    if not text:
        raise ValueError("Ollama response content is empty.")

    candidates: list[tuple[str, str]] = [("raw", text)]
    fenced_blocks = re.findall(r"```(?:json)?\s*(.*?)\s*```", text, flags=re.DOTALL | re.IGNORECASE)
    for block in fenced_blocks:
        snippet = block.strip()
        if snippet:
            candidates.append(("code_fence", snippet))

    first_brace = text.find("{")
    last_brace = text.rfind("}")
    if first_brace != -1 and last_brace != -1 and last_brace > first_brace:
        candidates.append(("brace_slice", text[first_brace : last_brace + 1]))

    for mode, candidate in candidates:
        try:
            parsed = json.loads(candidate)
        except Exception:
            continue
        if isinstance(parsed, dict):
            return parsed, mode

    raise ValueError("Ollama response did not contain a JSON object.")


def _normalize_insights(payload: dict[str, Any]) -> dict[str, Any]:
    normalized = dict(payload)
    summary = normalized.get("summary")
    if summary is None:
        normalized["summary"] = ""
    else:
        normalized["summary"] = str(summary)

    for key in _INSIGHT_LIST_KEYS:
        value = normalized.get(key)
        if value is None:
            normalized[key] = []
        elif isinstance(value, list):
            normalized[key] = value
        else:
            normalized[key] = [value]
    return normalized


def ollama_chat(
    messages: list[dict[str, str]],
    host: str,
    model: str,
    temperature: float,
    max_tokens: int,
    timeout_s: float = DEFAULT_OLLAMA_TIMEOUT_S,
) -> dict[str, Any]:
    payload = {
        "model": model,
        "messages": messages,
        "stream": False,
        "format": "json",
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
    timeout_s: float | None = None,
) -> dict[str, Any]:
    current_metrics = load_metrics(current)
    summary = compact_metrics_summary(current_metrics)
    comparison_result = None
    if baseline is not None:
        comparison_result = compare_and_score(current_metrics, baseline)

    host = ollama_host or os.environ.get("OLLAMA_HOST", DEFAULT_OLLAMA_HOST)
    model_name = model or os.environ.get("OLLAMA_MODEL", DEFAULT_OLLAMA_MODEL)
    timeout_seconds = _resolve_timeout_s(timeout_s)
    model_candidates = _build_model_candidates(model_name)

    backend = {
        "provider": "ollama",
        "host": host,
        "model": model_name,
        "status": "pending",
        "used_ollama": False,
        "error": None,
        "attempted_models": [],
        "request_timeout_s": timeout_seconds,
    }

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
    raw_content: Any = None
    for idx, candidate in enumerate(model_candidates):
        backend["attempted_models"].append(candidate)
        try:
            raw_response = ollama_chat(
                messages=messages,
                host=host,
                model=candidate,
                temperature=temperature,
                max_tokens=max_tokens,
                timeout_s=timeout_seconds,
            )
            raw_content = raw_response.get("message", {}).get("content", "")
            parsed_content, parse_mode = _parse_ollama_content(raw_content)
            parsed = _normalize_insights(parsed_content)
            backend["status"] = "ok"
            backend["used_ollama"] = True
            backend["model"] = candidate
            backend["parse_mode"] = parse_mode
            backend["model_alias_used"] = candidate != model_name
            return {
                "insights": parsed,
                "comparison": comparison_result,
                "raw_llm": raw_content if return_raw else None,
                "backend": backend,
                "heuristic_fallback": False,
            }
        except Exception as exc:
            backend["error"] = str(exc)
            should_try_alias = (
                idx + 1 < len(model_candidates)
                and _is_model_not_found_error(str(exc))
            )
            if should_try_alias:
                continue
            break

    backend["status"] = "fallback"
    fallback = _fallback_insights(summary, comparison_result)
    return {
        "insights": fallback,
        "comparison": comparison_result,
        "raw_llm": (raw_content if raw_content is not None else raw_response)
        if return_raw
        else None,
        "backend": backend,
        "heuristic_fallback": True,
    }
