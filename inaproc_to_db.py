import argparse
import asyncio
import csv
import json
import re
import sys
from pathlib import Path
from urllib.parse import urljoin

import psycopg2
from psycopg2.extras import RealDictCursor
from playwright.async_api import async_playwright
from playwright.async_api import TimeoutError as PlaywrightTimeoutError


# ============================================================
# DATABASE CONFIG
# ============================================================

DB_HOST = "localhost"
DB_PORT = 5432
DB_NAME = "inaproc"
DB_USER = "tryfatur"
DB_PASSWORD = "@Dmin123!"

DB_SCHEMA = "tender"
DB_TABLE = "details"
DB_KEY_FIELD = "kode_paket"

BASE_URL = "https://data.inaproc.id/realisasi?sumber=Tender&kode={kode_paket}"


# ============================================================
# FIELD MAPPING
# ============================================================

SCRAPE_TO_DB_FIELD_MAP = {
    "Cara Pembayaran": "cara_pembayaran",
    "Instansi": "instansi",
    "Kualifikasi Usaha": "kualifikasi_usaha",
    "Lokasi Pekerjaan": "lokasi_pekerjaan",
    "Metode Evaluasi": "metode_evaluasi",
    "Nilai HPS": "nilai_hps",
    "Nilai Pagu": "nilai_pagu",
    "Satuan Kerja": "satuan_kerja",
    "Tanggal Tender": "tanggal_tender",
}


# ============================================================
# TEXT HELPERS
# ============================================================

def normalize_header(text: str) -> str:
    """
    Untuk deteksi header tabel.
    Contoh:
    - "No." menjadi "no"
    - "Deskripsi" menjadi "deskripsi"
    - "Detail" menjadi "detail"
    """
    text = text or ""
    text = re.sub(r"\s+", " ", text).strip().lower()
    text = re.sub(r"[^\w\s]", "", text)
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def normalize_label(text: str) -> str:
    """
    Untuk mencocokkan label Deskripsi.
    Value hasil scrape tidak diubah selain strip whitespace tepi.
    """
    text = text or ""
    text = re.sub(r"\s+", " ", text).strip()
    return text


def make_unique_key(existing_keys: set, key: str) -> str:
    base_key = normalize_label(key)

    if not base_key:
        base_key = "Unnamed"

    final_key = base_key
    counter = 2

    while final_key in existing_keys:
        final_key = f"{base_key}_{counter}"
        counter += 1

    existing_keys.add(final_key)
    return final_key


# ============================================================
# CHECKPOINT + CSV BACKUP
# ============================================================

def append_checkpoint(checkpoint_path: Path, row: dict) -> None:
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
    print(f"CSV backup berhasil disimpan: {output_csv}")


# ============================================================
# DATABASE HELPERS
# ============================================================

def get_db_connection():
    return psycopg2.connect(
        host=DB_HOST,
        port=DB_PORT,
        dbname=DB_NAME,
        user=DB_USER,
        password=DB_PASSWORD,
    )


def fetch_kode_paket_list(
    conn,
    nama_instansi: str,
    tahun_anggaran: str,
    start: int,
    limit: int | None,
) -> list[str]:
    query = f"""
        SELECT {DB_KEY_FIELD}
        FROM {DB_SCHEMA}.{DB_TABLE}
        WHERE nama_instansi = %s
          AND tahun_anggaran = %s
        ORDER BY {DB_KEY_FIELD}
    """

    params = [nama_instansi, tahun_anggaran]

    if limit is not None:
        query += " LIMIT %s OFFSET %s"
        params.extend([limit, start])
    elif start > 0:
        query += " OFFSET %s"
        params.append(start)

    with conn.cursor(cursor_factory=RealDictCursor) as cursor:
        cursor.execute(query, params)
        rows = cursor.fetchall()

    return [str(row[DB_KEY_FIELD]) for row in rows if row.get(DB_KEY_FIELD)]


def build_update_payload(scraped_data: dict) -> dict:
    """
    Hanya field yang ada dalam mapping yang dikirim ke database.
    Kalau field tidak ditemukan dari hasil scrape, field tersebut tidak di-update.
    """
    payload = {}

    for scrape_label, db_field in SCRAPE_TO_DB_FIELD_MAP.items():
        if scrape_label in scraped_data:
            payload[db_field] = scraped_data.get(scrape_label)

    return payload


def update_detail_row(
    conn,
    kode_paket: str,
    nama_instansi: str,
    tahun_anggaran: str,
    payload: dict,
) -> tuple[bool, str, int]:
    if not payload:
        return False, "Tidak ada field hasil scrape yang cocok dengan mapping database.", 0

    set_clauses = []
    values = []

    for db_field, value in payload.items():
        set_clauses.append(f"{db_field} = %s")
        values.append(value)

    values.extend([
        kode_paket,
        nama_instansi,
        tahun_anggaran,
    ])

    query = f"""
        UPDATE {DB_SCHEMA}.{DB_TABLE}
        SET {", ".join(set_clauses)}
        WHERE {DB_KEY_FIELD} = %s
          AND nama_instansi = %s
          AND tahun_anggaran = %s
    """

    try:
        with conn.cursor() as cursor:
            cursor.execute(query, values)
            affected_rows = cursor.rowcount

        conn.commit()

        if affected_rows == 0:
            return False, "Tidak ada row database yang ter-update.", affected_rows

        return True, "", affected_rows

    except Exception as exc:
        conn.rollback()
        return False, str(exc), 0


# ============================================================
# PLAYWRIGHT SCRAPER
# ============================================================

async def extract_target_table(page, source_url: str, kode_paket: str) -> dict:
    result = {
        "kode_paket": kode_paket,
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

            desc_text = normalize_label(await desc_cell.inner_text())
            detail_text = (await detail_cell.inner_text()).strip()

            if not desc_text:
                continue

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


async def scrape_url(context, kode_paket: str, timeout_ms: int) -> dict:
    url = BASE_URL.format(kode_paket=kode_paket)

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

        data = await extract_target_table(
            page=page,
            source_url=url,
            kode_paket=kode_paket,
        )

        return data

    except Exception as exc:
        return {
            "kode_paket": kode_paket,
            "source_url": url,
            "scrape_status": "failed",
            "scrape_error": str(exc),
        }

    finally:
        try:
            await page.close()
        except Exception:
            pass


# ============================================================
# MAIN
# ============================================================

async def main():
    parser = argparse.ArgumentParser(
        description=(
            "Ambil kode_paket dari PostgreSQL, scrape halaman Inaproc, "
            "transpose tabel No | Deskripsi | Detail, lalu UPDATE tender.details."
        )
    )

    parser.add_argument(
        "--nama-instansi",
        required=True,
        help="Filter nama_instansi di database.",
    )

    parser.add_argument(
        "--tahun-anggaran",
        required=True,
        help="Filter tahun_anggaran di database.",
    )

    parser.add_argument(
        "--output",
        default="output_scraping_transposed.csv",
        help="Path CSV backup dari checkpoint.",
    )

    parser.add_argument(
        "--checkpoint",
        default="checkpoint_scraping_db.jsonl",
        help="Path checkpoint JSONL.",
    )

    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Jumlah kode_paket yang diproses. Kosongkan untuk semua data hasil query.",
    )

    parser.add_argument(
        "--start",
        type=int,
        default=0,
        help="Offset query kode_paket dari database. Default: 0.",
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

    nama_instansi = args.nama_instansi
    tahun_anggaran = args.tahun_anggaran
    output_csv = Path(args.output)
    checkpoint_path = Path(args.checkpoint)

    conn = None
    browser = None
    context = None

    try:
        print("Membuka koneksi PostgreSQL...")
        conn = get_db_connection()

        kode_paket_list = fetch_kode_paket_list(
            conn=conn,
            nama_instansi=nama_instansi,
            tahun_anggaran=tahun_anggaran,
            start=args.start,
            limit=args.limit,
        )

        if not kode_paket_list:
            raise ValueError(
                "Tidak ada kode_paket ditemukan untuk filter "
                f"nama_instansi='{nama_instansi}' dan tahun_anggaran='{tahun_anggaran}'."
            )

        print(f"Total kode_paket yang akan diproses: {len(kode_paket_list)}")

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

            for idx, kode_paket in enumerate(kode_paket_list, start=1):
                print(f"[{idx}/{len(kode_paket_list)}] Scraping kode_paket: {kode_paket}")

                scraped_data = await scrape_url(
                    context=context,
                    kode_paket=kode_paket,
                    timeout_ms=args.timeout,
                )

                scraped_data["db_update_status"] = "skipped"
                scraped_data["db_update_error"] = ""
                scraped_data["db_affected_rows"] = 0

                if scraped_data.get("scrape_status") == "success":
                    payload = build_update_payload(scraped_data)

                    success, error, affected_rows = update_detail_row(
                        conn=conn,
                        kode_paket=kode_paket,
                        nama_instansi=nama_instansi,
                        tahun_anggaran=tahun_anggaran,
                        payload=payload,
                    )

                    scraped_data["db_update_status"] = "success" if success else "failed"
                    scraped_data["db_update_error"] = error
                    scraped_data["db_affected_rows"] = affected_rows

                    if success:
                        print(f"    DB updated. affected_rows={affected_rows}")
                    else:
                        print(f"    DB update failed: {error}")

                else:
                    print(f"    Scrape failed: {scraped_data.get('scrape_error')}")

                append_checkpoint(
                    checkpoint_path=checkpoint_path,
                    row=scraped_data,
                )

    except KeyboardInterrupt:
        print("\nProses dihentikan oleh user dengan Ctrl+C.")
        print("Menyimpan hasil terakhir dari checkpoint ke CSV backup...")

    except Exception as exc:
        print(f"\nTerjadi error utama: {exc}")
        print("Menyimpan hasil terakhir dari checkpoint ke CSV backup...")

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

        try:
            if conn:
                conn.close()
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