import json
import urllib.error

import etl_agent
import pytest


def _sample_metrics():
    return {
        "metadata": {"etl_path": "test.etl"},
        "trace": {
            "duration_s": 10.0,
            "event_count": 100,
            "events_per_s": 10.0,
            "process_count": 4,
            "user_process_count": 3,
        },
        "boot": {
            "boot_duration_s": 4.0,
            "explorer_start_s": 3.0,
            "first_user_app_s": 5.0,
            "boot_order_count": 1,
            "boot_order": [{"image": "explorer.exe", "start_s": 3.0}],
        },
        "io": {
            "total_ops": 100,
            "total_bytes": 2048,
            "avg_bytes_per_op": 20.48,
            "slow_ops": 12,
            "slow_ops_pct": 0.12,
            "slow_time_s": 1.2,
            "slow_time_pct": 0.12,
            "percentiles_s": {"p95_s": 0.5, "p99_s": 1.0},
        },
        "launch_latency": {
            "stats": {"avg_s": 0.8, "p95_s": 1.0},
            "top": [{"image": "app.exe"}],
        },
        "top_processes": {
            "by_slow_time": [{"image": "app.exe", "slow_time_s": 1.0}],
            "by_slow_ops": [{"image": "app.exe", "slow_ops": 3}],
            "by_io_bytes": [{"image": "app.exe", "io_bytes": 1000}],
        },
        "top_files": [{"file": "C:/file", "slow_time_s": 1.0}],
        "process_lifetimes": [{"image": "app.exe", "lifetime_s": 9.0}],
    }


@pytest.fixture(autouse=True)
def _stub_local_models(monkeypatch):
    monkeypatch.setattr(etl_agent, "_list_local_ollama_models", lambda host, timeout_s: [])


def test_compact_summary_shape():
    summary = etl_agent.compact_metrics_summary(_sample_metrics(), top_n=1)
    assert "trace" in summary
    assert "io" in summary
    assert "boot" in summary
    assert summary["trace"]["events_per_s"] == 10.0
    assert summary["trace"]["process_count"] == 4
    assert summary["trace"]["user_process_count"] == 3
    assert summary["boot"]["boot_order_count"] == 1
    assert summary["io"]["slow_ops_pct"] == 0.12
    assert len(summary["top_processes"]["by_slow_time"]) == 1
    assert len(summary["top_files"]) == 1


def test_review_fallback(monkeypatch):
    def _boom(*args, **kwargs):
        raise RuntimeError("ollama down")

    monkeypatch.setattr(etl_agent, "ollama_chat", _boom)
    result = etl_agent.review_with_llm(_sample_metrics())
    assert result["heuristic_fallback"] is True
    assert result["backend"]["status"] == "fallback"
    assert "insights" in result
    assert "summary" in result["insights"]


def test_review_uses_ollama_backend_when_available(monkeypatch):
    def _ok(**kwargs):
        return {
            "message": {
                "content": json.dumps(
                    {
                        "summary": "Healthy run.",
                        "regressions": [],
                        "improvements": [],
                        "observations": ["Slow I/O stable."],
                        "recommendations": ["Keep current thresholds."],
                        "questions": [],
                    }
                )
            }
        }

    monkeypatch.setattr(etl_agent, "ollama_chat", _ok)
    result = etl_agent.review_with_llm(_sample_metrics())
    assert result["heuristic_fallback"] is False
    assert result["backend"]["status"] == "ok"
    assert result["backend"]["used_ollama"] is True
    assert result["insights"]["summary"] == "Healthy run."


def test_review_retries_ministral_alias_when_model_missing(monkeypatch):
    seen_models: list[str] = []

    def _chat(**kwargs):
        model = kwargs["model"]
        seen_models.append(model)
        if model == "ministral-3:latest":
            raise RuntimeError("model 'ministral-3:latest' not found, try pulling it first")
        return {
            "message": {
                "content": json.dumps(
                    {
                        "summary": "Alias model responded.",
                        "regressions": [],
                        "improvements": [],
                        "observations": [],
                        "recommendations": [],
                        "questions": [],
                    }
                )
            }
        }

    monkeypatch.setattr(etl_agent, "ollama_chat", _chat)
    result = etl_agent.review_with_llm(_sample_metrics(), model="ministral-3:latest")

    assert result["heuristic_fallback"] is False
    assert seen_models[:2] == ["ministral-3:latest", "ministral:latest"]
    assert result["backend"]["model"] == "ministral:latest"
    assert result["backend"]["model_alias_used"] is True


def test_review_retries_typo_model_with_mistral_alias(monkeypatch):
    seen_models: list[str] = []

    def _chat(**kwargs):
        model = kwargs["model"]
        seen_models.append(model)
        if model != "mistral:latest":
            raise RuntimeError("pull model manifest: file does not exist")
        return {
            "message": {
                "content": json.dumps(
                    {
                        "summary": "Recovered via mistral alias.",
                        "regressions": [],
                        "improvements": [],
                        "observations": [],
                        "recommendations": [],
                        "questions": [],
                    }
                )
            }
        }

    monkeypatch.setattr(etl_agent, "ollama_chat", _chat)
    result = etl_agent.review_with_llm(
        _sample_metrics(),
        model="ministral:latest",
        use_cli_fallback=False,
    )

    assert result["heuristic_fallback"] is False
    assert result["backend"]["model"] == "mistral:latest"
    assert "mistral:latest" in seen_models


def test_review_parses_code_fenced_json(monkeypatch):
    def _chat(**kwargs):
        return {
            "message": {
                "content": """```json
{
  "summary": "Parsed from fence.",
  "regressions": [],
  "improvements": [],
  "observations": ["All good."],
  "recommendations": [],
  "questions": []
}
```"""
            }
        }

    monkeypatch.setattr(etl_agent, "ollama_chat", _chat)
    result = etl_agent.review_with_llm(_sample_metrics())

    assert result["heuristic_fallback"] is False
    assert result["backend"]["parse_mode"] == "code_fence"
    assert result["insights"]["summary"] == "Parsed from fence."


def test_review_parses_plain_text_insights(monkeypatch):
    def _chat(**kwargs):
        return {
            "message": {
                "content": """Summary
This run is stable.

Observations
- Slow I/O is low

Recommendations
- Keep current tuning"""
            }
        }

    monkeypatch.setattr(etl_agent, "ollama_chat", _chat)
    result = etl_agent.review_with_llm(_sample_metrics(), use_cli_fallback=False)

    assert result["heuristic_fallback"] is False
    assert result["backend"]["parse_mode"] == "text_relaxed"
    assert result["insights"]["summary"] == "This run is stable."
    assert "Slow I/O is low" in result["insights"]["observations"]


def test_review_ignores_junk_text_and_retries_next_alias(monkeypatch):
    seen_models: list[str] = []

    def _chat(**kwargs):
        model = kwargs["model"]
        seen_models.append(model)
        if model in {"ministral:latest", "ministral"}:
            raise RuntimeError("pull model manifest: file does not exist")
        if model == "ministral-3:latest":
            return {"message": {"content": "{"}}
        if model == "mistral:latest":
            return {
                "message": {
                    "content": json.dumps(
                        {
                            "summary": "Recovered after skipping invalid payload.",
                            "regressions": [],
                            "improvements": [],
                            "observations": ["Model output is now valid JSON."],
                            "recommendations": [],
                            "questions": [],
                        }
                    )
                }
            }
        raise AssertionError(f"Unexpected model {model}")

    monkeypatch.setattr(etl_agent, "ollama_chat", _chat)
    result = etl_agent.review_with_llm(
        _sample_metrics(),
        model="ministral:latest",
        use_cli_fallback=False,
    )

    assert result["heuristic_fallback"] is False
    assert result["backend"]["model"] == "mistral:latest"
    assert result["backend"].get("error") is None
    assert result["backend"].get("cli_error") is None
    assert "mistral:latest" in seen_models


def test_review_retries_legacy_endpoint_when_api_chat_404(monkeypatch):
    class _Response:
        def __init__(self, body: dict[str, object]) -> None:
            self._payload = json.dumps(body).encode("utf-8")

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def read(self, *args, **kwargs):
            return self._payload

    seen_urls: list[str] = []

    def _urlopen(request, timeout=0):
        seen_urls.append(request.full_url)
        if request.full_url.endswith("/api/chat"):
            raise urllib.error.HTTPError(
                request.full_url,
                404,
                "Not Found",
                hdrs=None,
                fp=None,
            )
        if request.full_url.endswith("/api/generate"):
            return _Response(
                {
                    "response": json.dumps(
                        {
                            "summary": "Legacy endpoint worked.",
                            "regressions": [],
                            "improvements": [],
                            "observations": [],
                            "recommendations": [],
                            "questions": [],
                        }
                    )
                }
            )
        raise AssertionError(f"Unexpected URL {request.full_url}")

    monkeypatch.setattr(etl_agent.urllib.request, "urlopen", _urlopen)
    result = etl_agent.review_with_llm(_sample_metrics(), model="ministral:latest")

    assert result["heuristic_fallback"] is False
    assert result["insights"]["summary"] == "Legacy endpoint worked."
    assert result["backend"]["endpoint"] == "/api/generate"
    assert "/api/chat" in result["backend"]["attempted_endpoints"]
    assert "/api/generate" in result["backend"]["attempted_endpoints"]
    assert seen_urls[0].endswith("/api/chat")


def test_review_uses_cli_fallback_when_http_endpoints_fail(monkeypatch):
    def _http_fail(**kwargs):
        raise etl_agent.OllamaRequestError(
            "/api/chat: HTTP Error 404: Not Found; /api/generate: HTTP Error 404: Not Found",
            ["/api/chat", "/api/generate"],
        )

    def _cli_ok(**kwargs):
        return (
            {
                "message": {
                    "content": json.dumps(
                        {
                            "summary": "CLI fallback succeeded.",
                            "regressions": [],
                            "improvements": [],
                            "observations": [],
                            "recommendations": [],
                            "questions": [],
                        }
                    )
                }
            },
            {
                "endpoint": "ollama_cli",
                "attempted_endpoints": ["ollama_cli"],
                "transport": "cli",
            },
        )

    monkeypatch.setattr(etl_agent, "ollama_chat", _http_fail)
    monkeypatch.setattr(etl_agent, "_run_ollama_cli", _cli_ok)

    result = etl_agent.review_with_llm(_sample_metrics(), model="ministral:latest")

    assert result["heuristic_fallback"] is False
    assert result["backend"]["transport"] == "cli"
    assert result["backend"]["endpoint"] == "ollama_cli"
    assert result["backend"]["cli_fallback_used"] is True
    assert "/api/chat" in result["backend"]["attempted_endpoints"]
    assert "ollama_cli" in result["backend"]["attempted_endpoints"]


def test_review_uses_discovered_local_model_after_cli_timeout(monkeypatch):
    def _http_fail(**kwargs):
        raise etl_agent.OllamaRequestError(
            "/api/chat: HTTP Error 404: Not Found; /api/generate: HTTP Error 404: Not Found",
            ["/api/chat", "/api/generate"],
        )

    def _cli_run(**kwargs):
        model = kwargs["model"]
        if model == "mistral:latest":
            raise etl_agent.OllamaCliError("ollama CLI timed out after 90.0s.")
        if model == "mistral":
            return (
                {
                    "message": {
                        "content": json.dumps(
                            {
                                "summary": "Recovered using locally available mistral.",
                                "regressions": [],
                                "improvements": [],
                                "observations": [],
                                "recommendations": [],
                                "questions": [],
                            }
                        )
                    }
                },
                {
                    "endpoint": "ollama_cli",
                    "attempted_endpoints": ["ollama_cli"],
                    "transport": "cli",
                },
            )
        raise etl_agent.OllamaCliError("ollama CLI failed: pull model manifest: file does not exist")

    monkeypatch.setattr(
        etl_agent,
        "_list_local_ollama_models",
        lambda host, timeout_s: ["mistral"],
    )
    monkeypatch.setattr(etl_agent, "ollama_chat", _http_fail)
    monkeypatch.setattr(etl_agent, "_run_ollama_cli", _cli_run)

    result = etl_agent.review_with_llm(_sample_metrics(), model="ministral:latest")

    assert result["heuristic_fallback"] is False
    assert result["backend"]["transport"] == "cli"
    assert result["backend"]["model"] == "mistral"
    assert result["backend"]["error"] is None
    assert result["backend"]["cli_error"] is None
