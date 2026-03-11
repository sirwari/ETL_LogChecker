# ETL_LogChecker Tech Stack And Functions

## Runtime Stack
- Language: Python 3.11+
- UI: Tkinter (`ttk`, `scrolledtext`)
- Data formats: ETL, JSON, CSV, HTML, XML, PNG
- Optional LLM integration: Ollama HTTP API
- Optional Windows fallback: `tracerpt.exe`

## Required Python Dependencies
- `etl-parser`: ETL parsing and event access
- `mcp`: MCP server runtime for tool exposure

## Optional Python Dependencies
- `pandas`: prettier CLI tables
- `matplotlib`: network throughput plots
- `pytest`: test execution
- `playwright`: screenshot capture
- `Pillow`: screenshot fallback rendering

## Standard Library Modules Used Heavily
- `argparse`
- `csv`
- `datetime`
- `json`
- `math`
- `os`
- `subprocess`
- `sys`
- `tempfile`
- `tkinter`
- `urllib.request`
- `xml.etree.ElementTree`

## Primary Files
- `etl_logchecker.py`: core ETL parsing, UX analysis, HTML reporting, CLI, and GUI
- `etl_agent.py`: comparison scoring and Ollama-backed review logic
- `etl_runner.py`: stable Python entry points for analysis, comparison, and review
- `etl_local_cli.py`: local interactive text runner
- `etl_mcp_server.py`: MCP tool server over stdio
- `scripts/capture_screenshots.py`: documentation and PR proof screenshot generation

## Core Classes And Functions

### `etl_logchecker.py`
- `ETLAnalyzer`: low-level ETL parsing, provider filtering, and fallback handling
- `ETLUXAnalyzer`: UX/performance metrics builder for trace, I/O, launch, and boot data
- `run_analysis_job(...)`: main orchestration path used by CLI, GUI, and `etl_runner.py`
- `_render_report(...)`: self-contained HTML report renderer
- `launch_standalone_gui(...)`: Tk GUI entry point
- `render_gui_quickstart_text()`: reusable quick-start instructions for the GUI
- `_build_analysis_summary(...)`: human-readable summary used by the GUI and screenshots

### `etl_agent.py`
- `compact_metrics_summary(...)`: trims metrics for prompt payloads and summaries
- `compare_and_score(...)`: compares one current run against one baseline
- `compare_many(...)`: ranks multiple current runs against one baseline
- `review_with_llm(...)`: Ollama-backed review with heuristic fallback

### `etl_runner.py`
- `analyze_etl(...)`: wrapper around `run_analysis_job(...)`
- `compare_metrics(...)`: wrapper around comparison helpers
- `review_metrics(...)`: wrapper around review helpers

### `etl_mcp_server.py`
- Exposes `analyze_etl`, `compare_metrics`, and `review_metrics` as MCP tools

## Metric Model

### Trace
- `start_ts`
- `end_ts`
- `duration_s`
- `event_count`
- `events_per_s`
- `process_count`
- `user_process_count`

### I/O
- `total_ops`
- `total_bytes`
- `avg_bytes_per_op`
- `slow_ops`
- `slow_ops_pct`
- `slow_time_s`
- `slow_time_pct`
- `percentiles_s`
- `histogram`

### Launch Latency
- `top`
- `stats.count`
- `stats.avg_s`
- `stats.p50_s`
- `stats.p95_s`
- `stats.p99_s`

### Boot
- `boot_start_s`
- `smss_start_s`
- `wininit_start_s`
- `winlogon_start_s`
- `logonui_start_s`
- `explorer_start_s`
- `first_user_app_s`
- `boot_duration_s`
- `boot_order_count`
- `boot_order`

## Output Surfaces
- JSON metrics files written by `--metrics-output`
- HTML report written by `--report`
- CSV or JSON timelines written by `--timeline-output`
- Plot images written by `--plot-dir`
- XML event dumps written by `--xml-output`
- GUI tabs for Summary, Metrics JSON, Baseline JSON, Comparison, and Report HTML

## External Integration Points
- Ollama REST API: `POST /api/chat` (with automatic fallback to `/api/generate` and `/v1/chat/completions` when needed)
- Local `ollama` CLI fallback (`ollama run`) when HTTP endpoints fail
- MCP stdio transport
- Windows `tracerpt.exe`

## Operational Notes
- The analyzer is designed to stream ETL events instead of loading the whole file.
- Agentic Diagnose always returns structured data, even when Ollama is unavailable.
- The helper scripts (`install.*`, `start.*`) standardize setup for macOS, Linux, and Windows.
