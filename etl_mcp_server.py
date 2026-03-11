from __future__ import annotations

import asyncio
from typing import Any

import anyio
from mcp.server import Server
from mcp.server.stdio import stdio_server

from etl_runner import analyze_etl as _analyze_etl
from etl_runner import compare_metrics as _compare_metrics
from etl_runner import review_metrics as _review_metrics


server = Server("etl-logchecker")


@server.tool()
async def analyze_etl(
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
    return await asyncio.to_thread(
        _analyze_etl,
        etl_path,
        report_path,
        metrics_output_path,
        compare_metrics_path,
        compare_etl_path,
        timeline_output_path,
        timeline_format,
        plot_dir,
        slow_io_ms,
        top_n,
        time_scale,
        bootlog,
        boot_window_s,
        return_metrics,
        return_baseline_metrics,
    )


@server.tool()
async def compare_metrics(
    current: dict[str, Any] | str | list[dict[str, Any] | str],
    baseline: dict[str, Any] | str,
) -> dict[str, Any]:
    return _compare_metrics(current, baseline)


@server.tool()
async def review_metrics(
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
    return _review_metrics(
        current=current,
        baseline=baseline,
        ollama_host=ollama_host,
        model=model,
        temperature=temperature,
        max_tokens=max_tokens,
        focus=focus,
        return_raw=return_raw,
        timeout_s=timeout_s,
        use_cli_fallback=use_cli_fallback,
    )


async def main() -> None:
    async with stdio_server() as (read_stream, write_stream):
        await server.run(read_stream, write_stream)


if __name__ == "__main__":
    anyio.run(main)
