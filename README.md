# ETL_LogChecker

Python 3.11+ CLI and MCP server for analyzing Windows Performance Recorder `.etl` files.
It uses `etl-parser` with an optional `tracerpt.exe` fallback when providers are missing.

## Functionality
- Streams ETL events (no full in-memory load) for large traces.
- Filters `Microsoft-Windows-Kernel-Process` (GUID `22fb2cd6-0e7b-422b-a0c7-2fad1fd0e716`)
  and `Microsoft-Windows-Kernel-Network`.
- Maps `ProcessStart` to `ImageName`, tracks `ProcessStop`, and estimates CPU time
  as process lifetime (start -> stop).
- Aggregates network bytes sent/received per PID.
- Outputs a summary table using `pandas` if available, or a formatted ASCII table.
- Optional full XML event dump for downstream processing.
- Optional UX/performance report with boot milestones, app launch latency proxies,
  disk I/O wait time, and slow I/O offenders.
- Optional timeline export (CSV/JSON) and throughput plots.
- Built-in comparison against baseline metrics or a baseline ETL.
- MCP tools for agentic workflows plus Ollama-backed review summaries.
- Robust error handling for locked/corrupted ETL files.

## Requirements
- Python 3.11+
- `pip install -r requirements.txt`
- Optional: `pip install pandas` for prettier tables.
- Optional: `pip install matplotlib` for plot output.
- Optional: `pip install pytest` for running tests.
- Windows only for `tracerpt.exe` fallback (assumed on PATH).

## Usage
```bash
python etl_logchecker.py <path-to-etl>
```

Quick start scripts:
```bash
./install.sh
./start.sh
```

Windows PowerShell:
```powershell
.\install.ps1
.\start.ps1
```

## Examples
UX/Performance report + metrics JSON:
```bash
python etl_logchecker.py <path-to-etl> \
  --report report.html \
  --metrics-output etl_metrics.json
```

Compare against a baseline metrics file:
```bash
python etl_logchecker.py <path-to-etl> \
  --report report_compare.html \
  --compare etl_metrics_baseline.json
```

Compare two ETLs directly:
```bash
python etl_logchecker.py <path-to-etl> \
  --compare-etl <baseline.etl> \
  --report report_compare.html
```

Bootlog analysis with timeline export:
```bash
python etl_logchecker.py bootLog.etl \
  --bootlog --boot-window-s 300 \
  --report boot_report.html \
  --metrics-output boot_metrics.json \
  --timeline-output boot_timeline.csv --timeline-format csv
```

Export per-process timeline JSON:
```bash
python etl_logchecker.py <path-to-etl> \
  --timeline-output timeline.json --timeline-format json
```

Generate network throughput plot:
```bash
python etl_logchecker.py <path-to-etl> \
  --plot-dir plots
```

Force time-scale override:
```bash
python etl_logchecker.py <path-to-etl> --time-scale ms
```

Full XML event dump for downstream processing (can be large):
```bash
python etl_logchecker.py <path-to-etl> --xml-output out.xml
```

## Local Usage (No MCP)
Run the interactive local runner to call all major functions without MCP:
```bash
python etl_local_cli.py
```

Launch the standalone desktop GUI to analyze ETLs, compare metrics, and review runs in one window:
```bash
python etl_logchecker.py --gui
```

Example menu:
```
ETL Local Runner
1. Analyze ETL (report/metrics/timeline/plot/bootlog)
2. Compare metrics JSON
3. Compare ETL vs baseline ETL
4. Review metrics via Ollama
5. Quit
```

Programmatic local usage (no MCP):
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

## MCP Server (Agentic Use)
This repo includes an MCP server that exposes ETL analysis + comparison tools over stdio.

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

Ollama environment variables (optional):
- `OLLAMA_HOST` (default: `http://localhost:11434`)
- `OLLAMA_MODEL` (default: `gptoss20b`)

## Agentic Example With Ollama
1. Start Ollama:
```bash
ollama serve
```
2. Pull the model (if needed):
```bash
ollama pull gptoss20b
```
3. Call `review_metrics` via MCP:
```json
{
  "tool": "review_metrics",
  "arguments": {
    "current": "etl_metrics.json",
    "baseline": "etl_metrics_baseline.json",
    "focus": "Boot duration and slow I/O regressions",
    "temperature": 0.2,
    "max_tokens": 800
  }
}
```

## Agentic Examples (MCP Tool Calls)
Analyze + generate report and metrics:
```json
{
  "tool": "analyze_etl",
  "arguments": {
    "etl_path": "current.etl",
    "report_path": "report.html",
    "metrics_output_path": "etl_metrics.json",
    "bootlog": true,
    "boot_window_s": 300
  }
}
```

Compare multiple runs against a baseline:
```json
{
  "tool": "compare_metrics",
  "arguments": {
    "current": ["run1.json", "run2.json", "run3.json"],
    "baseline": "baseline.json"
  }
}
```

## Proof Screenshots (Dev)
```bash
pip install -r requirements-dev.txt
python scripts/capture_screenshots.py
```

If Playwright cannot download browsers, point it to a system Chrome:
```bash
export PLAYWRIGHT_CHROME="/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
```

If headless Chrome fails with macOS permission errors (e.g., `MachPortRendezvousServer`),
the script falls back to text-based PNGs using Pillow. For full browser-rendered
screenshots, run the capture script on a local machine with normal GUI permissions.

## Output Columns
- `PID`
- `Process Name`
- `CPU Time (est, s)`
- `Network I/O (sent/recv bytes)`

## Options
- `--output <path>`: write output to a file (default: stdout)
- `--xml-output <path>`: write a full XML event dump to a file
- `--format auto|pandas|table` (default: auto)
- `--sort cpu|net|pid` (default: cpu)
- `--max-rows <n>` (default: 50)
- `--tracerpt-exe <path>`: override tracerpt path
- `--no-tracerpt`: disable fallback
- `--no-etl-observer`: disable etl observer fallback (loads entire file into memory)
- `--force-etl-observer`: force etl observer fallback even for large ETL files
- `--report <path>`: write a self-contained HTML UX/performance report
- `--metrics-output <path>`: write a metrics JSON file for later comparison
- `--compare <metrics.json>`: compare against a baseline metrics file in the report
- `--compare-etl <baseline.etl>`: compare against a baseline ETL file in the report
- `--slow-io-ms <n>`: slow I/O threshold in milliseconds (default: 50)
- `--top-n <n>`: top N rows in report sections (default: 10)
- `--time-scale <auto|ns|us|ms|s|float>`: timestamp scale override
- `--timeline-output <path>`: write per-process timeline to a file
- `--timeline-format <csv|json>`: timeline output format (default: json)
- `--plot-dir <path>`: write matplotlib plots to this directory
- `--plot-bin-s <n>`: time bin size for plots in seconds (default: 1.0)
- `--bootlog`: enable bootlog-specific metrics and report sections
- `--boot-window-s <n>`: boot window length for boot order capture (default: 300)
- `--ux-progress-every <n>`: log UX analysis progress every N events (debug only)
- `--debug`: verbose logging

## Known Limitations
- CPU time is estimated from ProcessStart -> ProcessStop (not true CPU usage).
- Network direction is inferred from event naming; unknown direction is tracked
  but not displayed separately.
- `etl-parser` event schema can vary by version; the parser probes common APIs.

## Plan
See `PLAN.md` for the roadmap and next steps.
