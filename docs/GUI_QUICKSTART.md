# ETL_LogChecker GUI Quick Start

## 1. Install

### macOS Or Linux
```bash
./install.sh
```

### Windows PowerShell
```powershell
.\install.ps1
```

The install scripts create `.venv`, upgrade `pip`, and install `requirements.txt`.
Use `--dev` with the install script if you also want screenshot tooling.

## 2. Launch The GUI

### macOS Or Linux
```bash
./start.sh
```

### Windows PowerShell
```powershell
.\start.ps1
```

Direct Python launch also works:

```bash
python etl_logchecker.py --gui
```

You can print the built-in quick start at any time:

```bash
python etl_logchecker.py --gui-quickstart
```

## 3. Run Your First Analysis
- Open the `Analyze ETL` tab.
- Choose the ETL trace you want to inspect.
- Leave `Auto-create report, metrics, timeline, and plot outputs` enabled for the fastest setup.
- Click `Run Analysis`.
- Review the generated Summary, Metrics JSON, Comparison, and Report HTML tabs.

## 4. Compare Existing Runs
- Open the `Compare Metrics` tab.
- Select a current metrics JSON file.
- Select a baseline metrics JSON file.
- Click `Compare Metrics` to view the scored deltas.

## 5. Use Agentic Diagnose
- Open the `Agentic Diagnose` tab.
- Select the current metrics JSON and, optionally, a baseline metrics JSON.
- Leave the default `OLLAMA_HOST` and `OLLAMA_MODEL` values unless you need a custom setup.
- If the first model name is not available locally, the app retries known `ministral` aliases automatically.
- Keep `Use local ollama CLI fallback if HTTP endpoints fail` enabled unless you intentionally want HTTP-only behavior.
- Click `Run Agentic Diagnose`.

If Ollama is not reachable, the app falls back to the built-in heuristic summary
and shows that fallback status in the output.
If `/api/chat` is unavailable, the app retries compatible Ollama endpoints automatically.
If `ministral` names fail, the app retries equivalent `mistral` aliases.

## 6. Generated Files
With auto-output enabled, the GUI writes files in a timestamped folder named
`<trace>_YYYYMMDD_HHMM`:
- `<trace>_YYYYMMDD_HHMM/<trace>_report.html`
- `<trace>_YYYYMMDD_HHMM/<trace>_metrics.json`
- `<trace>_YYYYMMDD_HHMM/<trace>_timeline.json`
- `<trace>_YYYYMMDD_HHMM/<trace>_plots/`

## 7. Troubleshooting
- If the GUI does not open, verify that Tkinter is available in your Python build.
- If plots are missing, install `matplotlib`.
- If Agentic Diagnose falls back unexpectedly, verify that Ollama is running and
  reachable at `http://localhost:11434`.
- On Windows, keep `tracerpt.exe` on `PATH` for fallback parsing support.
