# ETL_LogChecker

Python 3.11+ CLI for analyzing Windows Performance Recorder `.etl` files using
`etl-parser` with an optional `tracerpt.exe` fallback when providers are missing.

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
- Falls back to `tracerpt.exe` (CSV then XML) if required providers are missing.
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

### UX/Performance report
```bash
python etl_logchecker.py <path-to-etl> --report report.html --metrics-output etl_metrics.json
```

### Compare against a baseline
```bash
python etl_logchecker.py <path-to-etl> --report report.html --compare etl_metrics.json
```

### Compare two ETL files
```bash
python etl_logchecker.py <path-to-etl> --compare-etl <baseline.etl> --report report_compare.html
```

### Time-scale override
```bash
python etl_logchecker.py <path-to-etl> --time-scale ms
```

### Per-process timeline export
```bash
python etl_logchecker.py <path-to-etl> --timeline-output timeline.json --timeline-format json
```

### Plot output
```bash
python etl_logchecker.py <path-to-etl> --plot-dir plots
```

### Bootlog mode
```bash
python etl_logchecker.py <path-to-etl> --bootlog --boot-window-s 300
```

### Demo calls
Bootlog-focused HTML + metrics + timeline:
```bash
python etl_logchecker.py bootLog.etl \
  --bootlog --boot-window-s 300 \
  --report boot_report.html \
  --metrics-output boot_metrics.json \
  --timeline-output boot_timeline.csv --timeline-format csv
```

Compare two ETLs with plots:
```bash
python etl_logchecker.py current.etl \
  --compare-etl baseline.etl \
  --report report_compare.html \
  --plot-dir plots
```

Force time-scale and export timeline JSON:
```bash
python etl_logchecker.py trace.etl \
  --time-scale 1e-7 \
  --timeline-output timeline.json --timeline-format json
```

### Options
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

## MCP Server
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

## Output Columns
- `PID`
- `Process Name`
- `CPU Time (est, s)`
- `Network I/O (sent/recv bytes)`

## Known Limitations
- CPU time is estimated from ProcessStart -> ProcessStop (not true CPU usage).
- Network direction is inferred from event naming; unknown direction is tracked
  but not displayed separately.
- `etl-parser` event schema can vary by version; the parser probes common APIs.

## To Do
- Add richer CPU metrics once a consistent kernel provider schema is confirmed.
