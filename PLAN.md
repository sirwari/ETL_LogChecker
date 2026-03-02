# Plan

**Goals**
- Keep CLI output stable while expanding the UX/performance metrics.
- Provide reliable MCP tools for agentic workflows.
- Offer LLM-based summaries with deterministic fallbacks.

**Near-term**
- Add MCP integration tests for `analyze_etl`, `compare_metrics`, and `review_metrics`.
- Add a local interactive runner (non-MCP) for end-to-end usage.
- Add a shared runner module to avoid duplicated analysis logic across CLI/MCP/local usage.
- Capture proof screenshots for reports, comparisons, and local runner output.
- Improve boot and I/O anomaly detection heuristics.
- Provide structured JSON schemas for metrics and comparisons.

**Mid-term**
- Expand CPU metrics with validated kernel provider schemas.
- Add optional storage for run history and trend analysis.
- Improve report visualizations for regressions/improvements.

**Backlog**
- Add Windows service wrapper for scheduled runs.
- Support LFS-based artifact retention workflows.
- Add export adapters for data warehouses or BI tools.
