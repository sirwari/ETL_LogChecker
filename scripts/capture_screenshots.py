from __future__ import annotations

import html
import json
import os
import re
import sys
from pathlib import Path

from playwright.sync_api import sync_playwright
from PIL import Image, ImageDraw, ImageFont

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from etl_logchecker import _build_analysis_summary  # noqa: E402
from etl_local_cli import render_menu_text  # noqa: E402
from etl_runner import review_metrics  # noqa: E402
SCREENSHOT_DIR = ROOT / "docs" / "screenshots"
TMP_DIR = ROOT / "docs" / "screenshots" / "_tmp"


def _write_html(path: Path, body: str) -> None:
    path.write_text(
        "\n".join(
            [
                "<!doctype html>",
                "<html>",
                "<head>",
                "<meta charset='utf-8' />",
                "<style>",
                "body { font-family: Arial, sans-serif; padding: 24px; }",
                "pre { background: #111; color: #e6e6e6; padding: 16px; border-radius: 8px; }",
                "table { border-collapse: collapse; width: 100%; font-size: 12px; }",
                "th, td { border: 1px solid #ddd; padding: 6px 8px; }",
                "th { background: #f2f2f2; text-align: left; }",
                "</style>",
                "</head>",
                "<body>",
                body,
                "</body>",
                "</html>",
            ]
        ),
        encoding="utf-8",
    )


def _csv_to_table(csv_path: Path, max_rows: int = 80) -> str:
    rows = csv_path.read_text(encoding="utf-8", errors="replace").splitlines()
    if not rows:
        return "<p>No CSV data.</p>"
    headers = rows[0].split(",")
    body_rows = rows[1 : max_rows + 1]
    header_html = "".join(f"<th>{h}</th>" for h in headers)
    body_html = []
    for row in body_rows:
        cols = row.split(",")
        body_html.append("".join(f"<td>{c}</td>" for c in cols))
    return (
        "<h2>Timeline CSV (preview)</h2>"
        "<table><thead><tr>"
        + header_html
        + "</tr></thead><tbody>"
        + "".join(f"<tr>{r}</tr>" for r in body_html)
        + "</tbody></table>"
    )


def _capture_page(page, html_path: Path, output_path: Path) -> None:
    page.goto(html_path.as_uri())
    page.set_viewport_size({"width": 1400, "height": 900})
    page.wait_for_timeout(500)
    page.screenshot(path=str(output_path), full_page=True)


def _strip_html(text: str) -> str:
    cleaned = re.sub(r"<[^>]+>", " ", text)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    return cleaned


def _render_text_image(text: str, output_path: Path) -> None:
    font = ImageFont.load_default()
    max_width = 1400
    padding = 20

    words = text.split()
    lines: list[str] = []
    line = ""
    draw_img = Image.new("RGB", (max_width, 100), "white")
    draw = ImageDraw.Draw(draw_img)

    for word in words:
        test_line = f"{line} {word}".strip()
        width = draw.textlength(test_line, font=font)
        if width > max_width - 2 * padding:
            lines.append(line)
            line = word
        else:
            line = test_line
    if line:
        lines.append(line)

    line_height = 16
    height = padding * 2 + line_height * max(len(lines), 1)
    image = Image.new("RGB", (max_width, height), "white")
    draw = ImageDraw.Draw(image)
    y = padding
    for line in lines:
        draw.text((padding, y), line, fill="black", font=font)
        y += line_height
    image.save(output_path)


def _render_gui_mock_image(
    summary_text: str,
    metrics: dict[str, object],
    output_path: Path,
) -> None:
    width = 1520
    height = 980
    image = Image.new("RGB", (width, height), "#e7edf5")
    draw = ImageDraw.Draw(image)
    font = ImageFont.load_default()

    def box(x1: int, y1: int, x2: int, y2: int, fill: str, outline: str | None = None) -> None:
        draw.rounded_rectangle((x1, y1, x2, y2), radius=18, fill=fill, outline=outline)

    def write(text: str, x: int, y: int, fill: str = "#102a43") -> None:
        draw.text((x, y), text, fill=fill, font=font)

    box(24, 20, width - 24, 84, "#102a43")
    write("ETL LogChecker Standalone GUI", 44, 42, "#f0f4f8")
    write(
        "Readable layout, inline help, and auto-generated output files.",
        310,
        42,
        "#d9e2ec",
    )

    box(24, 108, 460, height - 24, "#d7e0eb")

    box(40, 126, 444, 370, "#f8fbff")
    write("Trace & Baseline", 60, 146)
    write(
        "Pick the ETL to inspect. Baseline inputs are optional and only used for delta comparison.",
        60,
        172,
        "#486581",
    )
    source_labels = [
        "ETL trace",
        "Baseline metrics",
        "Baseline ETL",
    ]
    y = 208
    for label in source_labels:
        write(label, 60, y)
        box(60, y + 18, 420, y + 56, "#ffffff", "#bcccdc")
        y += 70

    box(40, 392, 444, 676, "#f8fbff")
    write("Output Files", 60, 412)
    write(
        "Auto mode creates report, metrics, timeline, and plot outputs next to the ETL using the ETL name.",
        60,
        438,
        "#486581",
    )
    box(60, 472, 388, 504, "#1d4ed8")
    box(394, 474, 420, 502, "#ffffff")
    write("Auto-create analysis files", 72, 482, "#ffffff")
    output_labels = [
        "Report output (readonly)",
        "Metrics output (readonly)",
        "Timeline output (readonly)",
        "Plot directory (readonly)",
    ]
    y = 528
    for label in output_labels:
        write(label, 60, y)
        box(60, y + 18, 420, y + 52, "#eef4fb", "#cbd5e1")
        y += 58

    box(40, 698, 444, height - 40, "#f8fbff")
    write("Settings", 60, 718)
    write(
        "Defaults are safe. Raise Top N for larger tables and enable debug only when needed.",
        60,
        744,
        "#486581",
    )
    settings_labels = [
        "Slow I/O threshold",
        "Top N",
        "Time scale",
        "Timeline format",
        "Bootlog / debug options",
    ]
    y = 780
    for label in settings_labels:
        write(label, 60, y)
        y += 34

    box(60, height - 98, 420, height - 52, "#1f6feb")
    write("Run Analysis", 188, height - 82, "#ffffff")

    box(484, 108, width - 24, 326, "#ffffff")
    write("Analysis Summary", 512, 132)
    summary_lines = summary_text.splitlines()[:9]
    summary_y = 168
    for line in summary_lines:
        write(line[:130], 512, summary_y, "#334e68")
        summary_y += 22

    trace = metrics.get("trace", {}) if isinstance(metrics.get("trace"), dict) else {}
    io = metrics.get("io", {}) if isinstance(metrics.get("io"), dict) else {}
    boot = metrics.get("boot", {}) if isinstance(metrics.get("boot"), dict) else {}
    cards = [
        ("Trace", f"{trace.get('duration_s', 'n/a')} s"),
        ("Events", str(trace.get("event_count", "n/a"))),
        ("Slow I/O", f"{io.get('slow_time_s', 'n/a')} s"),
        ("Boot", f"{boot.get('boot_duration_s', 'n/a')} s"),
    ]
    card_x = 484
    for title, value in cards:
        box(card_x, 350, card_x + 236, 454, "#ffffff")
        write(title, card_x + 24, 372, "#486581")
        write(value, card_x + 24, 406)
        card_x += 248

    box(484, 478, width - 24, 534, "#ffffff")
    tab_labels = ["Summary", "Metrics JSON", "Baseline JSON", "Comparison", "Report HTML"]
    tab_x = 512
    for idx, label in enumerate(tab_labels):
        fill = "#dbeafe" if idx == 0 else "#e5e7eb"
        box(tab_x, 492, tab_x + 158, 520, fill)
        write(label, tab_x + 14, 500, "#1f2937")
        tab_x += 168

    box(484, 554, width - 24, height - 24, "#ffffff")
    write("Report HTML / Metrics Preview", 512, 578)
    metrics_text = json.dumps(metrics, indent=2).splitlines()[:18]
    metrics_y = 616
    for line in metrics_text:
        write(line[:145], 512, metrics_y, "#243b53")
        metrics_y += 20

    image.save(output_path)


def main() -> int:
    SCREENSHOT_DIR.mkdir(parents=True, exist_ok=True)
    TMP_DIR.mkdir(parents=True, exist_ok=True)

    local_menu_html = TMP_DIR / "local_menu.html"
    menu_text = render_menu_text()
    _write_html(local_menu_html, f"<h2>Local Runner Menu</h2><pre>{menu_text}</pre>")

    review_html = TMP_DIR / "review_output.html"
    review_data = review_metrics(
        current=str(ROOT / "etl_metrics.json"),
        baseline=str(ROOT / "etl_metricsBaseline.json"),
        focus="Boot duration and slow I/O regressions",
        return_raw=False,
    )
    review_pretty = json.dumps(review_data, indent=2)
    _write_html(review_html, f"<h2>Review Output</h2><pre>{review_pretty}</pre>")

    timeline_html = TMP_DIR / "timeline.html"
    timeline_csv = ROOT / "boot_timeline.csv"
    _write_html(timeline_html, _csv_to_table(timeline_csv))

    current_metrics = json.loads((ROOT / "etl_metrics.json").read_text(encoding="utf-8"))
    baseline_metrics = json.loads(
        (ROOT / "etl_metricsBaseline.json").read_text(encoding="utf-8")
    )
    gui_preview_html = TMP_DIR / "standalone_gui.html"
    gui_result = {
        "metrics": current_metrics,
        "baseline_metrics": baseline_metrics,
        "comparison": {},
        "metrics_path": str(ROOT / "etl_metrics.json"),
        "baseline_metrics_path": str(ROOT / "etl_metricsBaseline.json"),
        "report_path": str(ROOT / "boot_report.html"),
        "timeline_path": str(ROOT / "boot_timeline.csv"),
        "plot_paths": {},
        "warnings": [],
    }
    gui_summary = _build_analysis_summary(
        gui_result,
        etl_path=str(ROOT / "bootLog.etl"),
        compare_source=str(ROOT / "etl_metricsBaseline.json"),
    )
    gui_body = "\n".join(
        [
            "<h2>Standalone GUI Preview</h2>",
            "<div style='display:grid;grid-template-columns:380px 1fr;gap:24px;'>",
            "<section>",
            "<h3>Trace &amp; Baseline</h3>",
            "<p>Pick the ETL you want to inspect. Baseline inputs are optional and only used for delta comparison.</p>",
            "<table><tbody>",
            f"<tr><th>ETL trace</th><td>{html.escape(str(ROOT / 'bootLog.etl'))}</td></tr>",
            f"<tr><th>Baseline</th><td>{html.escape(str(ROOT / 'etl_metricsBaseline.json'))}</td></tr>",
            f"<tr><th>Baseline ETL</th><td>{html.escape(str(ROOT / 'Baseline.etl'))}</td></tr>",
            "</tbody></table>",
            "<h3>Output Files</h3>",
            "<p>Auto-create analysis files is enabled. Output paths are generated from the ETL name and kept read-only.</p>",
            "<table><tbody>",
            f"<tr><th>Report</th><td>{html.escape(str(ROOT / 'boot_report.html'))}</td></tr>",
            f"<tr><th>Metrics</th><td>{html.escape(str(ROOT / 'etl_metrics.json'))}</td></tr>",
            f"<tr><th>Timeline</th><td>{html.escape(str(ROOT / 'boot_timeline.csv'))}</td></tr>",
            f"<tr><th>Plot directory</th><td>{html.escape(str(ROOT / 'docs' / 'screenshots'))}</td></tr>",
            "</tbody></table>",
            "<h3>Settings</h3>",
            "<p>Defaults stay visible with short help text so common runs do not require manual path editing.</p>",
            "</section>",
            "<section>",
            "<h3>Analysis Summary</h3>",
            f"<pre>{html.escape(gui_summary)}</pre>",
            "<h3>Metrics JSON (preview)</h3>",
            f"<pre>{html.escape(json.dumps(current_metrics, indent=2)[:9000])}</pre>",
            "</section>",
            "</div>",
        ]
    )
    _write_html(gui_preview_html, gui_body)

    boot_report = ROOT / "boot_report.html"
    compare_report = ROOT / "reportCompare_testdieZweite.html"

    if not boot_report.exists():
        raise FileNotFoundError(f"Missing report: {boot_report}")
    if not compare_report.exists():
        raise FileNotFoundError(f"Missing report: {compare_report}")
    if not timeline_csv.exists():
        raise FileNotFoundError(f"Missing timeline CSV: {timeline_csv}")

    chrome_path = os.environ.get("PLAYWRIGHT_CHROME")

    chrome_home = TMP_DIR / "chrome_home"
    chrome_home.mkdir(parents=True, exist_ok=True)
    env = os.environ.copy()
    env["HOME"] = str(chrome_home)
    env["XDG_CONFIG_HOME"] = str(chrome_home / "config")
    env["XDG_CACHE_HOME"] = str(chrome_home / "cache")

    launch_args = [
        "--disable-crash-reporter",
        "--disable-breakpad",
        "--disable-crashpad",
    ]

    try:
        with sync_playwright() as p:
            if chrome_path and os.path.exists(chrome_path):
                browser = p.chromium.launch(
                    executable_path=chrome_path,
                    env=env,
                    args=launch_args,
                )
            else:
                browser = p.chromium.launch(env=env, args=launch_args)
            page = browser.new_page()
            _capture_page(page, local_menu_html, SCREENSHOT_DIR / "local_runner_menu.png")
            _capture_page(page, boot_report, SCREENSHOT_DIR / "report_boot.png")
            _capture_page(page, compare_report, SCREENSHOT_DIR / "report_compare.png")
            _capture_page(page, timeline_html, SCREENSHOT_DIR / "timeline_csv.png")
            _capture_page(page, review_html, SCREENSHOT_DIR / "review_output.png")
            _capture_page(
                page,
                gui_preview_html,
                SCREENSHOT_DIR / "feat-standalone-gui.png",
            )
            browser.close()
    except Exception:
        menu_text = render_menu_text()
        _render_text_image(menu_text, SCREENSHOT_DIR / "local_runner_menu.png")

        boot_text = _strip_html(boot_report.read_text(encoding="utf-8", errors="replace"))
        _render_text_image(boot_text[:5000], SCREENSHOT_DIR / "report_boot.png")

        compare_text = _strip_html(compare_report.read_text(encoding="utf-8", errors="replace"))
        _render_text_image(compare_text[:5000], SCREENSHOT_DIR / "report_compare.png")

        timeline_text = timeline_csv.read_text(encoding="utf-8", errors="replace")
        _render_text_image(timeline_text[:5000], SCREENSHOT_DIR / "timeline_csv.png")

        review_text = review_pretty
        _render_text_image(review_text, SCREENSHOT_DIR / "review_output.png")

        _render_gui_mock_image(
            gui_summary,
            current_metrics,
            SCREENSHOT_DIR / "feat-standalone-gui.png",
        )

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
