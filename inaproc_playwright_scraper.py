from __future__ import annotations

import argparse
import csv
import json
import re
import time
from pathlib import Path
from typing import Any

from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
from playwright.sync_api import sync_playwright


DEFAULT_URL = (
    "https://data.inaproc.id/realisasi?"
    "tahun=2025&jenis_klpd=3&jenis_klpd=4&jenis_klpd=5&sumber=Tender"
)


def clean_text(value: str | None) -> str:
    if not value:
        return ""
    return re.sub(r"\s+", " ", value).strip()


def unique_headers(headers: list[str], width: int) -> list[str]:
    if not headers:
        headers = [f"column_{index + 1}" for index in range(width)]

    result: list[str] = []
    used: dict[str, int] = {}
    for index in range(width):
        header = clean_text(headers[index] if index < len(headers) else "")
        if not header:
            header = f"column_{index + 1}"

        used[header] = used.get(header, 0) + 1
        result.append(header if used[header] == 1 else f"{header}_{used[header]}")
    return result


def extract_dataframe(page) -> list[dict[str, Any]]:
    """Extract the first HTML table with class dataframe from the current page."""
    table = page.locator("table.dataframe").first
    table.wait_for(state="attached", timeout=60_000)

    payload = table.evaluate(
        """
        (table) => {
            const headers = Array.from(table.querySelectorAll("thead th"))
                .map((cell) => cell.innerText.trim());

            const rows = Array.from(table.querySelectorAll("tbody tr")).map((row) =>
                Array.from(row.querySelectorAll("th, td")).map((cell) => {
                    const link = cell.querySelector("a[href]");
                    return {
                        text: cell.innerText.trim(),
                        href: link ? link.href : null
                    };
                })
            );

            return { headers, rows };
        }
        """
    )

    rows = payload["rows"]
    if not rows:
        return []

    width = max(len(row) for row in rows)
    headers = unique_headers(payload["headers"], width)

    records: list[dict[str, Any]] = []
    for row in rows:
        record: dict[str, Any] = {}
        for index, header in enumerate(headers):
            cell = row[index] if index < len(row) else {"text": "", "href": None}
            record[header] = clean_text(cell["text"])
            if cell.get("href"):
                record[f"{header}_url"] = cell["href"]
        records.append(record)

    return records


def get_page_label(page) -> str:
    labels = page.locator("text=/Halaman\\s+\\d+\\s+dari\\s+\\d+/i")
    if labels.count() == 0:
        return ""
    return clean_text(labels.first.inner_text(timeout=2_000))


def get_visible_row_count(page) -> int:
    return page.locator("table.dataframe tbody tr").count()


def set_entries_per_page(page, entries_per_page: int) -> None:
    if entries_per_page <= 0:
        return

    current_select = page.locator('input[aria-label*="Entri per halaman"]').first
    current_select.wait_for(state="attached", timeout=60_000)

    current_label = current_select.get_attribute("aria-label") or ""
    if re.search(rf"\bSelected\s+{entries_per_page}\b", current_label, re.I):
        return

    previous_row_count = get_visible_row_count(page)
    current_select.click()

    option = page.get_by_role("option", name=str(entries_per_page), exact=True)
    option.first.click(timeout=30_000)

    try:
        page.wait_for_function(
            """
            ([targetRows, oldRowCount]) => {
                const select = document.querySelector('input[aria-label*="Entri per halaman"]');
                const selected = select ? select.getAttribute("aria-label") || "" : "";
                const rowCount = document.querySelectorAll("table.dataframe tbody tr").length;
                const caption = Array.from(document.querySelectorAll("p"))
                    .map((node) => node.innerText.trim())
                    .find((text) => /^Showing\\s+\\d+\\s+of\\s+\\d+\\s+records$/i.test(text)) || "";

                return selected.includes(`Selected ${targetRows}`)
                    && (rowCount >= targetRows || rowCount !== oldRowCount || caption.includes(`Showing ${targetRows}`));
            }
            """,
            arg=[entries_per_page, previous_row_count],
            timeout=60_000,
        )
    except PlaywrightTimeoutError:
        page.wait_for_timeout(2_000)


def click_next_page(page) -> bool:
    next_button = page.get_by_role("button", name=re.compile("Berikutnya", re.I))
    if next_button.count() == 0:
        return False

    button = next_button.first
    if button.is_disabled():
        return False

    previous_label = get_page_label(page)
    previous_first_row = ""
    first_cell = page.locator("table.dataframe tbody tr:first-child td:first-child")
    if first_cell.count() > 0:
        previous_first_row = clean_text(first_cell.first.inner_text(timeout=2_000))

    button.click()

    try:
        page.wait_for_function(
            """
            ([oldLabel, oldFirstRow]) => {
                const label = Array.from(document.querySelectorAll("p"))
                    .map((node) => node.innerText.trim())
                    .find((text) => /Halaman\\s+\\d+\\s+dari\\s+\\d+/i.test(text)) || "";

                const firstCell = document.querySelector(
                    "table.dataframe tbody tr:first-child td:first-child"
                );
                const firstRow = firstCell ? firstCell.innerText.trim() : "";

                return (label && label !== oldLabel) || (firstRow && firstRow !== oldFirstRow);
            }
            """,
            arg=[previous_label, previous_first_row],
            timeout=60_000,
        )
    except PlaywrightTimeoutError:
        page.wait_for_timeout(2_000)

    return True


def scrape(
    url: str,
    max_pages: int,
    output: Path,
    output_format: str,
    headless: bool,
    delay_seconds: float,
    entries_per_page: int,
) -> list[dict[str, Any]]:
    all_records: list[dict[str, Any]] = []

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=headless)
        page = browser.new_page(viewport={"width": 1440, "height": 1000})
        page.goto(url, wait_until="domcontentloaded", timeout=120_000)
        page.locator("table.dataframe").first.wait_for(state="attached", timeout=120_000)
        set_entries_per_page(page, entries_per_page)

        for page_number in range(1, max_pages + 1):
            records = extract_dataframe(page)
            for record in records:
                record["_scraped_page"] = page_number
                record["_page_label"] = get_page_label(page)
            all_records.extend(records)

            print(f"Scraped page {page_number}: {len(records)} rows")

            if page_number >= max_pages:
                break

            if delay_seconds > 0:
                time.sleep(delay_seconds)

            if not click_next_page(page):
                break

        browser.close()

    save_records(all_records, output, output_format)
    return all_records


def save_records(records: list[dict[str, Any]], output: Path, output_format: str) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)

    if output_format == "json":
        output.write_text(
            json.dumps(records, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        return

    fieldnames: list[str] = []
    seen = set()
    for record in records:
        for key in record:
            if key not in seen:
                fieldnames.append(key)
                seen.add(key)

    with output.open("w", newline="", encoding="utf-8-sig") as file:
        writer = csv.DictWriter(file, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(records)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Scrape INAProc Streamlit table data from table.dataframe."
    )
    parser.add_argument("--url", default=DEFAULT_URL, help="Streamlit page URL to scrape.")
    parser.add_argument(
        "--max-pages",
        type=int,
        default=1,
        help="Maximum pages to scrape. The sample page has 50 rows per page.",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("output/inaproc_realisasi.csv"),
        help="Output file path.",
    )
    parser.add_argument(
        "--format",
        choices=["csv", "json"],
        default="csv",
        help="Output format.",
    )
    parser.add_argument(
        "--headful",
        action="store_true",
        help="Show the browser while scraping.",
    )
    parser.add_argument(
        "--delay",
        type=float,
        default=0.5,
        help="Delay between page clicks in seconds.",
    )
    parser.add_argument(
        "--entries-per-page",
        type=int,
        default=50,
        help="Value to select in the Entri per halaman control. Use 0 to leave unchanged.",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    records = scrape(
        url=args.url,
        max_pages=args.max_pages,
        output=args.output,
        output_format=args.format,
        headless=not args.headful,
        delay_seconds=args.delay,
        entries_per_page=args.entries_per_page,
    )
    print(f"Saved {len(records)} rows to {args.output}")


if __name__ == "__main__":
    main()
