import argparse
import asyncio
import csv
import json
import re
import sys
from pathlib import Path
from urllib.parse import urljoin

from playwright.async_api import async_playwright
from playwright.async_api import TimeoutError as PlaywrightTimeoutError


def normalize_text(text: str) -> str:
    return re.sub(r"\s+", " ", text or "").strip()


def normalize_header(text: str) -> str:
    """
    Normalisasi header agar:
    - "No" dan "No." dianggap sama
    - Kapitalisasi tidak berpengaruh
    - Simbol seperti titik dihapus
    """
    text = normalize_text(text).lower()
    text = re.sub(r"[^\w\s]", "", text)
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def make_unique_key(existing_keys: set, key: str) -> str:
    """
    Menghindari nama kolom duplikat.
    """
    base_key = normalize_text(key)

    if not base_key:
        base_key = "Unnamed"

    final_key = base_key
    counter = 2

    while final_key in existing_keys:
        final_key = f"{base_key}_{counter}"
        counter += 1

    existing_keys.add(final_key)
    return final_key


def append_checkpoint(checkpoint_path: Path, row: dict) -> None:
    """
    Simpan 1 hasil scraping ke JSONL.
    Setiap baris adalah 1 JSON object.
    """
    checkpoint_path.parent.mkdir(parents=True, exist_ok=True)

    with checkpoint_path.open("a", encoding="utf-8") as file:
        file.write(json.dumps(row, ensure_ascii=False) + "\n")
        file.flush()


def read_checkpoint(checkpoint_path: Path) -> list[dict]:
    rows = []

    if not checkpoint_path.exists():
        return rows

    with checkpoint_path.open("r", encoding="utf-8") as file:
        for line in file:
            line = line.strip()

            if not line:
                continue

            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                continue

    return rows


def write_output_csv(output_csv: Path, rows: list[dict]) -> None:
    if not rows:
        print("Tidak ada data untuk ditulis ke CSV.")
        return

    all_columns = []

    for row in rows:
        for key in row.keys():
            if key not in all_columns:
                all_columns.append(key)

    output_csv.parent.mkdir(parents=True, exist_ok=True)

    with output_csv.open("w", encoding="utf-8-sig", newline="") as file:
        writer = csv.DictWriter(file, fieldnames=all_columns)
        writer.writeheader()
        writer.writerows(rows)


def checkpoint_to_csv(checkpoint_path: Path, output_csv: Path) -> None:
    rows = read_checkpoint(checkpoint_path)

    if not rows:
        print("Belum ada data checkpoint yang bisa disimpan ke CSV.")
        return

    write_output_csv(output_csv, rows)
    print(f"CSV berhasil disimpan: {output_csv}")


def read_input_urls(
    input_csv: Path,
    url_column: str,
    start: int,
    limit: int | None,
) -> list[str]:
    urls = []

    with input_csv.open("r", encoding="utf-8-sig", newline="") as file:
        reader = csv.DictReader(file)

        if not reader.fieldnames:
            raise ValueError("CSV input tidak memiliki header.")

        if url_column not in reader.fieldnames:
            available_columns = ", ".join(reader.fieldnames)
            raise ValueError(
                f"Kolom URL '{url_column}' tidak ditemukan. "
                f"Kolom tersedia: {available_columns}"
            )

        for index, row in enumerate(reader):
            if index < start:
                continue

            url = normalize_text(row.get(url_column, ""))

            if url:
                urls.append(url)

            if limit is not None and len(urls) >= limit:
                break

    return urls


async def extract_target_table(page, source_url: str) -> dict:
    """
    Cari tabel dengan header:
    No | Deskripsi | Detail

    Lalu transpose:
    - data kolom Deskripsi menjadi header/kolom
    - data kolom Detail menjadi value
    """
    result = {
        "source_url": source_url,
        "scrape_status": "success",
        "scrape_error": "",
    }

    tables = page.locator("table")
    table_count = await tables.count()

    for table_index in range(table_count):
        table = tables.nth(table_index)

        rows = table.locator("tr")
        row_count = await rows.count()

        if row_count == 0:
            continue

        header_row_index = None
        header_map = {}

        for r in range(row_count):
            row = rows.nth(r)
            cells = row.locator("th, td")
            cell_count = await cells.count()

            headers = []

            for c in range(cell_count):
                cell_text = await cells.nth(c).inner_text()
                headers.append(normalize_header(cell_text))

            required_headers = {"no", "deskripsi", "detail"}

            if required_headers.issubset(set(headers)):
                header_row_index = r
                header_map = {
                    header: idx
                    for idx, header in enumerate(headers)
                }
                break

        if header_row_index is None:
            continue

        deskripsi_idx = header_map["deskripsi"]
        detail_idx = header_map["detail"]

        used_keys = set(result.keys())

        for r in range(header_row_index + 1, row_count):
            row = rows.nth(r)
            cells = row.locator("th, td")
            cell_count = await cells.count()

            if cell_count <= max(deskripsi_idx, detail_idx):
                continue

            desc_cell = cells.nth(deskripsi_idx)
            detail_cell = cells.nth(detail_idx)

            desc_text = normalize_text(await desc_cell.inner_text())
            detail_text = normalize_text(await detail_cell.inner_text())

            if not desc_text:
                continue

            # Playwright Python: .first adalah property, bukan function.
            link = detail_cell.locator("a[href]").first

            if await link.count() > 0:
                href = await link.get_attribute("href")

                if href:
                    detail_text = urljoin(source_url, href)

            output_key = make_unique_key(used_keys, desc_text)
            result[output_key] = detail_text

        return result

    result["scrape_status"] = "failed"
    result["scrape_error"] = (
        "Tabel dengan header No | Deskripsi | Detail tidak ditemukan"
    )

    return result


async def scrape_url(context, url: str, timeout_ms: int) -> dict:
    page = await context.new_page()

    try:
        await page.goto(
            url,
            wait_until="domcontentloaded",
            timeout=timeout_ms,
        )

        try:
            await page.wait_for_selector("table", timeout=timeout_ms)
        except PlaywrightTimeoutError:
            pass

        data = await extract_target_table(page, url)
        return data

    except Exception as exc:
        return {
            "source_url": url,
            "scrape_status": "failed",
            "scrape_error": str(exc),
        }

    finally:
        try:
            await page.close()
        except Exception:
            pass


async def main():
    parser = argparse.ArgumentParser(
        description=(
            "Scrape tabel No | Deskripsi | Detail dari daftar URL dalam CSV, "
            "transpose hasilnya, lalu simpan ke CSV."
        )
    )

    parser.add_argument(
        "--input",
        required=True,
        help="Path file CSV input.",
    )

    parser.add_argument(
        "--url-column",
        required=True,
        help="Nama kolom yang berisi URL.",
    )

    parser.add_argument(
        "--output",
        default="output_scraping_transposed.csv",
        help="Path file CSV output.",
    )

    parser.add_argument(
        "--checkpoint",
        default="checkpoint_scraping.jsonl",
        help="Path file checkpoint JSONL.",
    )

    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Jumlah URL yang diproses. Kosongkan untuk semua baris.",
    )

    parser.add_argument(
        "--start",
        type=int,
        default=0,
        help="Mulai proses dari index baris ke-nol. Default: 0.",
    )

    parser.add_argument(
        "--timeout",
        type=int,
        default=30000,
        help="Timeout Playwright dalam milidetik. Default: 30000.",
    )

    parser.add_argument(
        "--headless",
        action="store_true",
        help="Jalankan browser tanpa tampilan UI.",
    )

    args = parser.parse_args()

    input_csv = Path(args.input)
    output_csv = Path(args.output)
    checkpoint_path = Path(args.checkpoint)

    urls = read_input_urls(
        input_csv=input_csv,
        url_column=args.url_column,
        start=args.start,
        limit=args.limit,
    )

    if not urls:
        raise ValueError("Tidak ada URL yang dapat diproses dari CSV input.")

    browser = None
    context = None

    try:
        async with async_playwright() as p:
            browser = await p.chromium.launch(
                headless=args.headless
            )

            context = await browser.new_context(
                viewport={
                    "width": 1366,
                    "height": 768,
                },
                user_agent=(
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/120.0.0.0 Safari/537.36"
                ),
            )

            for idx, url in enumerate(urls, start=1):
                print(f"[{idx}/{len(urls)}] Scraping: {url}")

                row_result = await scrape_url(
                    context=context,
                    url=url,
                    timeout_ms=args.timeout,
                )

                append_checkpoint(checkpoint_path, row_result)

    except KeyboardInterrupt:
        print("\nProses dihentikan oleh user dengan Ctrl+C.")
        print("Menyimpan hasil terakhir dari checkpoint ke CSV...")

    except Exception as exc:
        print(f"\nTerjadi error utama: {exc}")
        print("Menyimpan hasil terakhir dari checkpoint ke CSV...")

    finally:
        try:
            if context:
                await context.close()
        except Exception:
            pass

        try:
            if browser:
                await browser.close()
        except Exception:
            pass

        checkpoint_to_csv(
            checkpoint_path=checkpoint_path,
            output_csv=output_csv,
        )

        print("Proses selesai.")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nProses dihentikan.")
        sys.exit(1)