from __future__ import annotations

import argparse
import os
from typing import Callable

from etl_runner import analyze_etl, compare_metrics, review_metrics


def _list_files(suffix: str) -> list[str]:
    return sorted([name for name in os.listdir(".") if name.endswith(suffix)])


def _prompt_path(label: str, suffix: str | None = None, default: str | None = None) -> str:
    candidates = _list_files(suffix) if suffix else []
    if candidates:
        print(f"\nAvailable {label}:")
        for idx, name in enumerate(candidates, start=1):
            print(f"  {idx}. {name}")
        print("  0. Enter a custom path")
        choice = input(f"Select {label} [default: {default or 'none'}]: ").strip()
        if choice.isdigit():
            choice_idx = int(choice)
            if choice_idx == 0:
                custom = input(f"Enter {label} path: ").strip()
                return custom or (default or "")
            if 1 <= choice_idx <= len(candidates):
                return candidates[choice_idx - 1]
    else:
        print(f"No {label} found in current directory.")
    path = input(f"Enter {label} path [default: {default or 'none'}]: ").strip()
    return path or (default or "")


def _prompt_optional_path(label: str, suffix: str | None = None) -> str | None:
    value = _prompt_path(label, suffix=suffix, default="").strip()
    return value or None


def _prompt_yes_no(label: str, default: bool = False) -> bool:
    default_text = "y" if default else "n"
    value = input(f"{label} (y/n) [default: {default_text}]: ").strip().lower()
    if not value:
        return default
    return value.startswith("y")


def _prompt_int(label: str, default: int) -> int:
    value = input(f"{label} [default: {default}]: ").strip()
    if not value:
        return default
    try:
        return int(value)
    except ValueError:
        print("Invalid value, using default.")
        return default


def _prompt_float(label: str, default: float) -> float:
    value = input(f"{label} [default: {default}]: ").strip()
    if not value:
        return default
    try:
        return float(value)
    except ValueError:
        print("Invalid value, using default.")
        return default


def render_menu_text() -> str:
    return "\n".join(
        [
            "ETL Local Runner",
            "1. Analyze ETL (report/metrics/timeline/plot/bootlog)",
            "2. Compare metrics JSON",
            "3. Compare ETL vs baseline ETL",
            "4. Agentic diagnose via Ollama",
            "5. Quit",
        ]
    )


def _handle_analyze() -> None:
    etl_path = _prompt_path("ETL file", suffix=".etl")
    report_path = _prompt_optional_path("report output (.html)")
    metrics_output_path = _prompt_optional_path("metrics output (.json)")
    timeline_output_path = _prompt_optional_path("timeline output (.json/.csv)")
    timeline_format = "json"
    if timeline_output_path:
        if timeline_output_path.endswith(".csv"):
            timeline_format = "csv"
    plot_dir = _prompt_optional_path("plot directory")
    compare_metrics_path = _prompt_optional_path("baseline metrics (.json)")
    compare_etl_path = None
    if not compare_metrics_path:
        compare_etl_path = _prompt_optional_path("baseline ETL (.etl)")
    slow_io_ms = _prompt_int("slow I/O threshold (ms)", 50)
    top_n = _prompt_int("top N rows", 10)
    time_scale = input("time scale (auto|ns|us|ms|s|float) [default: auto]: ").strip() or "auto"
    bootlog = _prompt_yes_no("enable bootlog", False)
    boot_window_s = _prompt_float("boot window length (s)", 300.0)

    result = analyze_etl(
        etl_path=etl_path,
        report_path=report_path,
        metrics_output_path=metrics_output_path,
        compare_metrics_path=compare_metrics_path,
        compare_etl_path=compare_etl_path,
        timeline_output_path=timeline_output_path,
        timeline_format=timeline_format,
        plot_dir=plot_dir,
        slow_io_ms=slow_io_ms,
        top_n=top_n,
        time_scale=time_scale,
        bootlog=bootlog,
        boot_window_s=boot_window_s,
    )
    print("\nAnalysis complete:")
    for key, value in result.items():
        if key in ("metrics", "baseline_metrics"):
            continue
        print(f"  {key}: {value}")


def _handle_compare_metrics() -> None:
    current_path = _prompt_path("current metrics (.json)", suffix=".json")
    baseline_path = _prompt_path("baseline metrics (.json)", suffix=".json")
    result = compare_metrics(current_path, baseline_path)
    print("\nComparison complete:")
    print(f"  comparisons: {len(result.get('comparisons', []))}")
    print(f"  ranked: {len(result.get('ranked', []))}")


def _handle_compare_etl() -> None:
    current_etl = _prompt_path("current ETL (.etl)", suffix=".etl")
    baseline_etl = _prompt_path("baseline ETL (.etl)", suffix=".etl")
    report_path = _prompt_optional_path("report output (.html)")
    metrics_output_path = _prompt_optional_path("metrics output (.json)")
    result = analyze_etl(
        etl_path=current_etl,
        compare_etl_path=baseline_etl,
        report_path=report_path,
        metrics_output_path=metrics_output_path,
    )
    print("\nETL comparison complete:")
    for key, value in result.items():
        if key in ("metrics", "baseline_metrics"):
            continue
        print(f"  {key}: {value}")


def _handle_review() -> None:
    current_path = _prompt_path("current metrics (.json)", suffix=".json")
    baseline_path = _prompt_optional_path("baseline metrics (.json)")
    focus = input("focus prompt (optional): ").strip() or None
    result = review_metrics(
        current=current_path,
        baseline=baseline_path,
        focus=focus,
    )
    print("\nReview complete:")
    backend = result.get("backend", {}) or {}
    print(f"  backend_status: {backend.get('status')}")
    print(f"  backend_model: {backend.get('model')}")
    print(f"  heuristic_fallback: {result.get('heuristic_fallback')}")
    print("  insights keys:", list(result.get("insights", {}).keys()))


def main() -> int:
    parser = argparse.ArgumentParser(description="Local interactive runner for ETL_LogChecker.")
    parser.add_argument(
        "--menu-only",
        action="store_true",
        help="Print the menu and exit (used for screenshots).",
    )
    args = parser.parse_args()

    if args.menu_only:
        print(render_menu_text())
        return 0

    actions: dict[str, Callable[[], None]] = {
        "1": _handle_analyze,
        "2": _handle_compare_metrics,
        "3": _handle_compare_etl,
        "4": _handle_review,
    }

    while True:
        print("\n" + render_menu_text())
        choice = input("Select an option: ").strip()
        if choice == "5":
            print("Goodbye.")
            return 0
        action = actions.get(choice)
        if not action:
            print("Invalid selection.")
            continue
        action()


if __name__ == "__main__":
    raise SystemExit(main())
