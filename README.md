# ETL_LogChecker

ETL_LogChecker is a Python 3.11+ analyzer for Windows Performance Recorder `.etl`
files. It provides a CLI, a standalone Tk GUI, and an MCP server for agentic
workflows. The core implementation lives in `etl_logchecker.py`.

## Documentation
- Full stack and function map: [docs/TECH_STACK_AND_FUNCTIONS.md](docs/TECH_STACK_AND_FUNCTIONS.md)
- GUI install and usage guide: [docs/GUI_QUICKSTART.md](docs/GUI_QUICKSTART.md)

## Quick Start
Use the built-in quick-start output at any time:

```bash
python etl_logchecker.py --gui-quickstart
```

Typical install and launch flow:

macOS/Linux:
```bash
./install.sh
./start.sh
```

Windows PowerShell:
```powershell
.\install.ps1
.\start.ps1
```

If you want to bypass the helper scripts:

```bash
python etl_logchecker.py --gui
```

## What The GUI Does
- Analyze one ETL and generate report, metrics JSON, timeline, and plots.
- Compare current metrics JSON against a saved baseline.
- Run Agentic Diagnose with Ollama-backed review or the built-in heuristic fallback.
- Auto-create output file paths next to the selected ETL trace.

## Core Functionality
- Streams ETL events instead of loading the full trace into memory.
- Falls back to `tracerpt.exe` when provider parsing is unavailable on Windows.
- Tracks process lifetime, network I/O, launch latency, and boot milestones.
- Generates HTML reports, JSON metrics, CSV/JSON timelines, and optional plots.
- Supports saved-baseline comparisons and baseline ETL comparisons.
- Exposes the same analysis flows through local Python APIs and an MCP server.

## Main Outputs
Analysis mode can produce:
- HTML report
- Metrics JSON
- Timeline export (`.json` or `.csv`)
- Network throughput plot
- Optional XML event dump

Key metrics surfaced in JSON and the HTML report include:
- `trace.duration_s`
- `trace.event_count`
- `trace.events_per_s`
- `trace.process_count`
- `trace.user_process_count`
- `io.slow_time_s`
- `io.slow_time_pct`
- `io.slow_ops`
- `io.slow_ops_pct`
- `io.avg_bytes_per_op`
- `launch_latency.stats.avg_s`
- `launch_latency.stats.p95_s`
- `boot.boot_duration_s`
- `boot.boot_order_count`

## Requirements
- Python 3.11+
- `pip install -r requirements.txt`
- Optional: `pip install pandas` for nicer CLI tables
- Optional: `pip install matplotlib` for plot output
- Optional: `pip install pytest` for local test runs
- Optional: local Ollama for Agentic Diagnose
- Windows only: `tracerpt.exe` fallback (expected on `PATH`)

## CLI Usage
Basic CLI analysis:

```bash
python etl_logchecker.py <path-to-etl>
```

Generate a report and metrics JSON:

```bash
python etl_logchecker.py <path-to-etl> \
  --report report.html \
  --metrics-output etl_metrics.json
```

Compare against a baseline metrics file:

```bash
python etl_logchecker.py <path-to-etl> \
  --compare etl_metrics_baseline.json \
  --report report_compare.html
```

Compare against a baseline ETL:

```bash
python etl_logchecker.py <path-to-etl> \
  --compare-etl baseline.etl \
  --report report_compare.html
```

Bootlog analysis with a timeline export:

```bash
python etl_logchecker.py bootLog.etl \
  --bootlog \
  --boot-window-s 300 \
  --report boot_report.html \
  --metrics-output boot_metrics.json \
  --timeline-output boot_timeline.csv \
  --timeline-format csv
```

## Local Runner
The local text runner exposes the same major flows without MCP:

```bash
python etl_local_cli.py
```

Menu:

```text
ETL Local Runner
1. Analyze ETL (report/metrics/timeline/plot/bootlog)
2. Compare metrics JSON
3. Compare ETL vs baseline ETL
4. Agentic diagnose via Ollama
5. Quit
```

## Programmatic Usage
```python
from etl_runner import analyze_etl, compare_metrics, review_metrics

result = analyze_etl(
    etl_path="bootLog.etl",
    report_path="boot_report.html",
    metrics_output_path="boot_metrics.json",
    bootlog=True,
    boot_window_s=300,
)

comparison = compare_metrics("etl_metrics.json", "etl_metricsBaseline.json")

review = review_metrics(
    current="etl_metrics.json",
    baseline="etl_metricsBaseline.json",
    focus="Boot duration and slow I/O regressions",
)
```

## MCP Server
Start the server:

```bash
python etl_mcp_server.py
```

Example MCP client config:

```json
{
  "mcpServers": {
    "etl-logchecker": {
      "command": "python",
      "args": ["/Users/zogthein/ETL_LogChecker/etl_mcp_server.py"]
    }
  }
}
```

Available tools:
- `analyze_etl`
- `compare_metrics`
- `review_metrics`

## Ollama Notes
Environment variables:
- `OLLAMA_HOST` (default: `http://localhost:11434`)
- `OLLAMA_MODEL` (default: `gptoss20b`)

Typical setup:

```bash
ollama serve
ollama pull gptoss20b
```

If Ollama is unavailable, Agentic Diagnose falls back to the built-in heuristic
summary and reports that status in the GUI and returned JSON.

## Dev Proof Screenshots
```bash
pip install -r requirements-dev.txt
python scripts/capture_screenshots.py
```

Feature screenshots used for PRs:
- [docs/screenshots/feat-standalone-gui.png](docs/screenshots/feat-standalone-gui.png)
- [docs/screenshots/feat-agentic-diagnose.png](docs/screenshots/feat-agentic-diagnose.png)
- [docs/screenshots/feat-gui-quickstart.png](docs/screenshots/feat-gui-quickstart.png)
- [docs/screenshots/feat-tech-stack-doc.png](docs/screenshots/feat-tech-stack-doc.png)
