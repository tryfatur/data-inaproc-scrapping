from __future__ import annotations

import argparse
import csv
import json
import re
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
from playwright.sync_api import sync_playwright


BASE_URL = "https://data.inaproc.id/realisasi"


def build_url(year: int) -> str:
    params = [
        ("tahun", str(year)),
        ("jenis_klpd", "3"),
        ("jenis_klpd", "4"),
        ("jenis_klpd", "5"),
        ("sumber", "Tender"),
    ]

    return f"{BASE_URL}?{urlencode(params)}"


def build_default_output_path(
    year: int,
    start_page: int,
    end_page: int,
    output_format: str,
) -> Path:
    return Path(f"output/inaproc_{year}_page_{start_page}_to_{end_page}.{output_format}")


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

        if used[header] == 1:
            result.append(header)
        else:
            result.append(f"{header}_{used[header]}")

    return result


def wait_for_table(page, timeout: int = 120_000) -> None:
    try:
        page.locator("table.dataframe").first.wait_for(
            state="attached",
            timeout=timeout,
        )
    except PlaywrightTimeoutError:
        page.screenshot(path="debug_table_not_found.png", full_page=True)

        print("Table tidak ditemukan.")
        print("Screenshot debug disimpan ke: debug_table_not_found.png")
        print(f"Page title: {page.title()}")
        print(f"Current URL: {page.url}")

        raise


def extract_dataframe(page) -> list[dict[str, Any]]:
    """
    Extract first HTML table with class dataframe from current page.
    """
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
                    && (
                        rowCount >= targetRows
                        || rowCount !== oldRowCount
                        || caption.includes(`Showing ${targetRows}`)
                    );
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


def go_to_start_page(
    page,
    start_page: int,
    delay_seconds: float,
) -> int:
    current_page = 1

    if start_page <= 1:
        return current_page

    print(f"Skipping dari page 1 ke page {start_page}...")

    while current_page < start_page:
        if not click_next_page(page):
            print(f"Tidak bisa lanjut ke page {current_page + 1}. Stop di page {current_page}.")
            return current_page

        current_page += 1

        print(f"Skipped to page {current_page}")

        if delay_seconds > 0:
            time.sleep(delay_seconds)

    return current_page


def scrape(
    url: str,
    start_page: int,
    end_page: int,
    output: Path,
    output_format: str,
    headless: bool,
    delay_seconds: float,
    entries_per_page: int,
) -> list[dict[str, Any]]:
    if start_page < 1:
        raise ValueError("--start-page harus minimal 1.")

    if end_page < start_page:
        raise ValueError("--end-page / --max-pages harus lebih besar atau sama dengan --start-page.")

    all_records: list[dict[str, Any]] = []

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(
            headless=headless,
            args=[
                "--no-sandbox",
                "--disable-dev-shm-usage",
            ],
        )

        page = browser.new_page(viewport={"width": 1440, "height": 1000})

        print(f"Opening URL: {url}")

        page.goto(url, wait_until="domcontentloaded", timeout=120_000)

        wait_for_table(page, timeout=120_000)

        set_entries_per_page(page, entries_per_page)

        reached_page = go_to_start_page(
            page=page,
            start_page=start_page,
            delay_seconds=delay_seconds,
        )

        if reached_page < start_page:
            print(f"Page awal yang diminta tidak tercapai. Dimulai dari page {reached_page}.")
            start_page = reached_page

        for page_number in range(start_page, end_page + 1):
            records = extract_dataframe(page)

            for record in records:
                record["_scraped_page"] = page_number
                record["_page_label"] = get_page_label(page)

            all_records.extend(records)

            print(f"Scraped page {page_number}: {len(records)} rows")

            if page_number >= end_page:
                break

            if delay_seconds > 0:
                time.sleep(delay_seconds)

            if not click_next_page(page):
                print("Tombol halaman berikutnya tidak ditemukan atau sudah disabled.")
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

    parser.add_argument(
        "--url",
        default=None,
        help="Custom URL override. Kalau tidak diisi, URL dibuat otomatis dari --year.",
    )

    parser.add_argument(
        "--year",
        type=int,
        default=2025,
        help="Parameter tahun untuk URL INAProc.",
    )

    parser.add_argument(
        "--start-page",
        type=int,
        default=1,
        help="Mulai scraping dari page ini.",
    )

    parser.add_argument(
        "--end-page",
        "--max-pages",
        dest="end_page",
        type=int,
        default=1,
        help="Page terakhir yang di-scrape. Alias: --max-pages.",
    )

    parser.add_argument(
        "--output",
        type=Path,
        default=None,
        help="Output file path. Kalau tidak diisi, nama file dibuat otomatis.",
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

    url = args.url or build_url(args.year)

    output = args.output or build_default_output_path(
        year=args.year,
        start_page=args.start_page,
        end_page=args.end_page,
        output_format=args.format,
    )

    records = scrape(
        url=url,
        start_page=args.start_page,
        end_page=args.end_page,
        output=output,
        output_format=args.format,
        headless=not args.headful,
        delay_seconds=args.delay,
        entries_per_page=args.entries_per_page,
    )

    print(f"Saved {len(records)} rows to {output}")


if __name__ == "__main__":
    main()