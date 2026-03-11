from __future__ import annotations

import json
import math
import os
import re
import subprocess
import urllib.error
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
DEFAULT_OLLAMA_USE_CLI_FALLBACK = True
_INSIGHT_LIST_KEYS = (
    "regressions",
    "improvements",
    "observations",
    "recommendations",
    "questions",
)
_ANSI_ESCAPE_RE = re.compile(r"\x1b(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])")
_SECTION_TITLES = (
    "summary",
    "observations",
    "regressions",
    "improvements",
    "recommendations",
    "questions",
)


class OllamaRequestError(RuntimeError):
    def __init__(self, message: str, attempted_endpoints: list[str]) -> None:
        super().__init__(message)
        self.attempted_endpoints = attempted_endpoints


class OllamaCliError(RuntimeError):
    pass


def _has_meaningful_text(value: Any) -> bool:
    text = "" if value is None else str(value).strip()
    if not text:
        return False
    if re.fullmatch(r"[{}\[\]`\"':,.;\s]+", text):
        return False
    if text.startswith("{") and not text.endswith("}"):
        return False
    if text.startswith("[") and not text.endswith("]"):
        return False
    return bool(re.search(r"[A-Za-z0-9]", text))


def _insights_have_signal(payload: dict[str, Any]) -> bool:
    if _has_meaningful_text(payload.get("summary")):
        return True
    for key in _INSIGHT_LIST_KEYS:
        items = payload.get(key)
        if isinstance(items, list) and any(_has_meaningful_text(item) for item in items):
            return True
    return False


def _parse_ollama_list(stdout: str) -> list[str]:
    models: list[str] = []
    for raw_line in stdout.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if line.lower().startswith("name"):
            continue
        name = line.split()[0].strip()
        if name and name not in models:
            models.append(name)
    return models


def _list_local_ollama_models(host: str, timeout_s: float) -> list[str]:
    env = os.environ.copy()
    if host:
        env["OLLAMA_HOST"] = host
    timeout = max(5.0, min(15.0, float(timeout_s)))
    try:
        result = subprocess.run(
            ["ollama", "list"],
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
            env=env,
        )
    except Exception:
        return []
    stdout = _sanitize_terminal_text(result.stdout)
    if not stdout:
        return []
    return _parse_ollama_list(stdout)


def _candidate_available_locally(candidate: str, local_models: list[str]) -> bool:
    normalized = candidate.strip().lower()
    available = {item.strip().lower() for item in local_models if str(item).strip()}
    if normalized in available:
        return True
    if normalized.endswith(":latest") and normalized[:-7] in available:
        return True
    if f"{normalized}:latest" in available:
        return True
    return False


def _prioritize_candidates_with_local_models(
    model_name: str,
    candidates: list[str],
    local_models: list[str],
) -> list[str]:
    if not local_models:
        return candidates

    deduped: list[str] = []

    def _add(value: str) -> None:
        text = value.strip()
        if text and text not in deduped:
            deduped.append(text)

    root = model_name.strip().lower().split(":", 1)[0]
    local_related = []
    for local_model in local_models:
        lower = local_model.lower()
        if root and (root in lower or ("ministral" in root and "mistral" in lower)):
            local_related.append(local_model)

    for candidate in candidates:
        if _candidate_available_locally(candidate, local_models):
            _add(candidate)

    for local_model in local_related:
        _add(local_model)

    for candidate in candidates:
        _add(candidate)

    return deduped


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


def _resolve_bool_env(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() not in {"0", "false", "no", "off", ""}


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
        _add("mistral:latest")
        _add("mistral")
    elif lower_name.startswith("ministral:"):
        _add("ministral")
        _add("ministral-3:latest")
        _add("mistral:latest")
        _add("mistral")
    elif lower_name.startswith("ministral-3"):
        _add("ministral:latest")
        _add("ministral")
        _add("mistral:latest")
        _add("mistral")
    elif lower_name == "mistral":
        _add("mistral:latest")
    elif lower_name.startswith("mistral:"):
        _add("mistral")

    return candidates


def _is_model_not_found_error(error_text: str) -> bool:
    text = str(error_text).lower()
    if "model" not in text:
        return False
    return (
        "not found" in text
        or "pull" in text
        or "does not exist" in text
        or "manifest" in text
        or "file does not exist" in text
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


def _sanitize_terminal_text(text: Any) -> str:
    value = "" if text is None else str(text)
    value = _ANSI_ESCAPE_RE.sub("", value)
    value = value.replace("\r", "\n").replace("\b", "")
    value = re.sub(r"\n{3,}", "\n\n", value)
    return value.strip()


def _parse_ollama_text_insights(content: Any) -> dict[str, Any] | None:
    if not isinstance(content, str):
        return None
    text = _sanitize_terminal_text(content)
    if not text:
        return None

    has_section_marker = any(
        re.search(rf"(^|\n)\s*{title}\s*:?\s*(\n|$)", text, flags=re.IGNORECASE)
        for title in _SECTION_TITLES
    )

    parsed: dict[str, Any] = {"summary": "", **{key: [] for key in _INSIGHT_LIST_KEYS}}
    current_section: str | None = None
    summary_lines: list[str] = []

    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        normalized = line.rstrip(":").strip().lower()
        if normalized in _SECTION_TITLES:
            current_section = normalized
            continue

        bullet = re.sub(r"^[\-*•]\s*", "", line).strip()
        if current_section == "summary":
            summary_lines.append(bullet)
            continue
        if current_section in _INSIGHT_LIST_KEYS:
            parsed[current_section].append(bullet)
            continue
        summary_lines.append(bullet)

    if summary_lines:
        parsed["summary"] = (
            " ".join(summary_lines[:2]) if has_section_marker else summary_lines[0]
        )

    if not parsed["summary"]:
        parsed["summary"] = text[:200]

    has_any_detail = any(parsed[key] for key in _INSIGHT_LIST_KEYS)
    if not has_any_detail and has_section_marker:
        parsed["observations"] = summary_lines[:5]
    if not _insights_have_signal(parsed):
        return None
    return parsed


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


def _flatten_messages_for_prompt(messages: list[dict[str, str]]) -> tuple[str, str]:
    system_parts: list[str] = []
    prompt_parts: list[str] = []
    for message in messages:
        role = str(message.get("role") or "").strip().lower()
        content = str(message.get("content") or "").strip()
        if not content:
            continue
        if role == "system":
            system_parts.append(content)
            continue
        if role in ("user", "assistant"):
            prompt_parts.append(content)
        elif role:
            prompt_parts.append(f"[{role}] {content}")
        else:
            prompt_parts.append(content)
    return "\n\n".join(system_parts), "\n\n".join(prompt_parts)


def _build_ollama_endpoint_requests(
    messages: list[dict[str, str]],
    model: str,
    temperature: float,
    max_tokens: int,
) -> list[tuple[str, dict[str, Any]]]:
    chat_payload = {
        "model": model,
        "messages": messages,
        "stream": False,
        "format": "json",
        "options": {
            "temperature": temperature,
            "num_predict": max_tokens,
        },
    }

    system_text, prompt_text = _flatten_messages_for_prompt(messages)
    if not prompt_text:
        prompt_text = "Return strict JSON analysis."
    generate_payload: dict[str, Any] = {
        "model": model,
        "prompt": prompt_text,
        "stream": False,
        "format": "json",
        "options": {
            "temperature": temperature,
            "num_predict": max_tokens,
        },
    }
    if system_text:
        generate_payload["system"] = system_text

    openai_payload = {
        "model": model,
        "messages": messages,
        "temperature": temperature,
        "max_tokens": max_tokens,
    }

    return [
        ("/api/chat", chat_payload),
        ("/api/generate", generate_payload),
        ("/v1/chat/completions", openai_payload),
    ]


def _is_endpoint_retryable_error(exc: Exception) -> bool:
    if isinstance(exc, urllib.error.HTTPError):
        return exc.code in (400, 404, 405, 415, 422, 500, 501)
    return isinstance(exc, ValueError)


def _extract_llm_content(raw_response: dict[str, Any]) -> tuple[Any, str]:
    message = raw_response.get("message")
    if isinstance(message, dict):
        if "content" in message:
            return message.get("content"), "chat"

    if "response" in raw_response:
        return raw_response.get("response"), "generate"

    choices = raw_response.get("choices")
    if isinstance(choices, list) and choices:
        first = choices[0]
        if isinstance(first, dict):
            message = first.get("message")
            if isinstance(message, dict) and "content" in message:
                return message.get("content"), "openai_chat"
            if "text" in first:
                return first.get("text"), "openai_text"

    raise ValueError("Ollama response did not expose a supported content field.")


def _should_try_cli_after_http_error(exc: Exception) -> bool:
    if isinstance(exc, OllamaRequestError):
        return True
    if isinstance(exc, urllib.error.URLError):
        return True
    if isinstance(exc, ValueError):
        return True
    return isinstance(exc, TimeoutError)


def _should_try_next_model(exc: Exception, error_text: str) -> bool:
    if _is_model_not_found_error(error_text):
        return True
    if isinstance(exc, ValueError):
        return True
    lowered = error_text.lower()
    if "did not contain a json object" in lowered:
        return True
    if "response content is empty" in lowered:
        return True
    if "http error 404" in lowered:
        return True
    if "timed out" in lowered or "timeout" in lowered:
        return True
    if "non-local model candidate" in lowered:
        return True
    return False


def _build_cli_prompt(messages: list[dict[str, str]]) -> str:
    system_text, prompt_text = _flatten_messages_for_prompt(messages)
    parts = []
    if system_text:
        parts.append(system_text)
    if prompt_text:
        parts.append(prompt_text)
    if not parts:
        parts.append("Provide a strict JSON response.")
    return "\n\n".join(parts)


def _run_ollama_cli(
    messages: list[dict[str, str]],
    host: str,
    model: str,
    timeout_s: float,
) -> tuple[dict[str, Any], dict[str, Any]]:
    prompt = _build_cli_prompt(messages)
    command = ["ollama", "run", model, prompt]
    env = os.environ.copy()
    if host:
        env["OLLAMA_HOST"] = host
    try:
        result = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=timeout_s,
            check=False,
            env=env,
        )
    except FileNotFoundError as exc:
        raise OllamaCliError("ollama CLI not found on PATH.") from exc
    except subprocess.TimeoutExpired as exc:
        raise OllamaCliError(f"ollama CLI timed out after {timeout_s:.1f}s.") from exc
    except Exception as exc:
        raise OllamaCliError(f"ollama CLI failed to start: {exc}") from exc

    stdout = _sanitize_terminal_text(result.stdout)
    stderr = _sanitize_terminal_text(result.stderr)
    if result.returncode != 0:
        details = stderr or stdout or f"exit code {result.returncode}"
        raise OllamaCliError(f"ollama CLI failed: {details}")
    if not stdout:
        raise OllamaCliError("ollama CLI returned an empty response.")

    return {"message": {"content": stdout}}, {
        "endpoint": "ollama_cli",
        "attempted_endpoints": ["ollama_cli"],
        "transport": "cli",
    }


def ollama_chat(
    messages: list[dict[str, str]],
    host: str,
    model: str,
    temperature: float,
    max_tokens: int,
    timeout_s: float = DEFAULT_OLLAMA_TIMEOUT_S,
) -> tuple[dict[str, Any], dict[str, Any]]:
    endpoint_requests = _build_ollama_endpoint_requests(
        messages=messages,
        model=model,
        temperature=temperature,
        max_tokens=max_tokens,
    )
    attempted_endpoints: list[str] = []
    errors: list[str] = []

    for idx, (path, payload) in enumerate(endpoint_requests):
        attempted_endpoints.append(path)
        data = json.dumps(payload).encode("utf-8")
        url = host.rstrip("/") + path
        request = urllib.request.Request(
            url,
            data=data,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout_s) as response:
                parsed = json.load(response)
            if not isinstance(parsed, dict):
                raise ValueError("Ollama response body is not a JSON object.")
            return parsed, {
                "endpoint": path,
                "attempted_endpoints": attempted_endpoints[:],
            }
        except Exception as exc:
            errors.append(f"{path}: {exc}")
            should_try_next = (
                idx + 1 < len(endpoint_requests)
                and _is_endpoint_retryable_error(exc)
            )
            if should_try_next:
                continue
            raise OllamaRequestError("; ".join(errors), attempted_endpoints[:]) from exc

    raise OllamaRequestError(
        "; ".join(errors) if errors else "No Ollama endpoint attempts were made.",
        attempted_endpoints[:],
    )


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
    use_cli_fallback: bool | None = None,
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
    cli_fallback_enabled = (
        bool(use_cli_fallback)
        if use_cli_fallback is not None
        else _resolve_bool_env("OLLAMA_USE_CLI_FALLBACK", DEFAULT_OLLAMA_USE_CLI_FALLBACK)
    )
    local_models = (
        _list_local_ollama_models(host, timeout_seconds) if cli_fallback_enabled else []
    )
    model_candidates = _prioritize_candidates_with_local_models(
        model_name=model_name,
        candidates=model_candidates,
        local_models=local_models,
    )

    backend = {
        "provider": "ollama",
        "host": host,
        "model": model_name,
        "status": "pending",
        "used_ollama": False,
        "error": None,
        "attempted_models": [],
        "attempted_endpoints": [],
        "request_timeout_s": timeout_seconds,
        "transport": "http",
        "cli_fallback_enabled": cli_fallback_enabled,
        "cli_attempted": False,
    }
    if local_models:
        backend["local_models"] = local_models[:20]

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
            chat_result = ollama_chat(
                messages=messages,
                host=host,
                model=candidate,
                temperature=temperature,
                max_tokens=max_tokens,
                timeout_s=timeout_seconds,
            )
            chat_meta: dict[str, Any] = {}
            if (
                isinstance(chat_result, tuple)
                and len(chat_result) == 2
                and isinstance(chat_result[1], dict)
            ):
                raw_response, chat_meta = chat_result
            else:
                raw_response = chat_result  # backward compatibility for monkeypatched tests

            attempted_endpoints = chat_meta.get("attempted_endpoints", [])
            if isinstance(attempted_endpoints, list):
                for endpoint in attempted_endpoints:
                    if endpoint not in backend["attempted_endpoints"]:
                        backend["attempted_endpoints"].append(endpoint)
            endpoint = chat_meta.get("endpoint")
            transport = chat_meta.get("transport")

            raw_content, response_source = _extract_llm_content(raw_response)
            try:
                parsed_content, parse_mode = _parse_ollama_content(raw_content)
            except ValueError:
                text_insights = _parse_ollama_text_insights(raw_content)
                if text_insights is None:
                    raise
                parsed_content = text_insights
                parse_mode = "text_relaxed"
            parsed = _normalize_insights(parsed_content)
            backend["status"] = "ok"
            backend["used_ollama"] = True
            backend["model"] = candidate
            backend["error"] = None
            backend["cli_error"] = None
            if endpoint:
                backend["endpoint"] = endpoint
            if transport:
                backend["transport"] = str(transport)
            backend["parse_mode"] = parse_mode
            backend["response_source"] = response_source
            backend["model_alias_used"] = candidate != model_name
            return {
                "insights": parsed,
                "comparison": comparison_result,
                "raw_llm": raw_content if return_raw else None,
                "backend": backend,
                "heuristic_fallback": False,
            }
        except Exception as exc:
            if isinstance(exc, OllamaRequestError):
                for endpoint in exc.attempted_endpoints:
                    if endpoint not in backend["attempted_endpoints"]:
                        backend["attempted_endpoints"].append(endpoint)
            backend["error"] = str(exc)

            if cli_fallback_enabled and _should_try_cli_after_http_error(exc):
                backend["cli_attempted"] = True
                if local_models and not _candidate_available_locally(candidate, local_models):
                    backend["cli_error"] = (
                        f"ollama CLI skipped non-local model candidate: {candidate}"
                    )
                    backend["error"] = f"{exc}; cli: {backend['cli_error']}"
                else:
                    try:
                        raw_response, cli_meta = _run_ollama_cli(
                            messages=messages,
                            host=host,
                            model=candidate,
                            timeout_s=timeout_seconds,
                        )
                        attempted_endpoints = cli_meta.get("attempted_endpoints", [])
                        if isinstance(attempted_endpoints, list):
                            for endpoint in attempted_endpoints:
                                if endpoint not in backend["attempted_endpoints"]:
                                    backend["attempted_endpoints"].append(endpoint)
                        endpoint = cli_meta.get("endpoint")
                        if endpoint:
                            backend["endpoint"] = endpoint
                        backend["transport"] = str(cli_meta.get("transport") or "cli")

                        raw_content, response_source = _extract_llm_content(raw_response)
                        parsed_content, parse_mode = _parse_ollama_content(raw_content)
                        parsed = _normalize_insights(parsed_content)
                        backend["status"] = "ok"
                        backend["used_ollama"] = True
                        backend["model"] = candidate
                        backend["error"] = None
                        backend["cli_error"] = None
                        backend["parse_mode"] = parse_mode
                        backend["response_source"] = response_source
                        backend["cli_fallback_used"] = True
                        backend["model_alias_used"] = candidate != model_name
                        return {
                            "insights": parsed,
                            "comparison": comparison_result,
                            "raw_llm": raw_content if return_raw else None,
                            "backend": backend,
                            "heuristic_fallback": False,
                        }
                    except Exception as cli_exc:
                        backend["cli_error"] = str(cli_exc)
                        backend["error"] = f"{exc}; cli: {cli_exc}"

            error_text = str(backend.get("error") or exc)
            should_try_alias = (
                idx + 1 < len(model_candidates)
                and _should_try_next_model(exc, error_text)
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
