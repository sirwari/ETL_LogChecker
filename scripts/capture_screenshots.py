from __future__ import annotations

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

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
