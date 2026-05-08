import argparse
import logging
import re
import time
from pathlib import Path
from datetime import datetime

import pandas as pd
from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError


BASE_URL = "https://spse.inaproc.id/"

TIMEOUT = 30000
RUN_ID = datetime.now().strftime("%Y%m%d_%H%M%S")

SPSE_SEARCH_SELECTORS = [
    'input[placeholder="Cari K/L/Pemda/instansi Lainnya atau SPSE"]',
    'input[placeholder*="Cari K/L/Pemda"]',
    'input[placeholder*="SPSE"]',
    'input[type="search"]',
]

Path("logs").mkdir(exist_ok=True)
Path("debug/screenshots").mkdir(parents=True, exist_ok=True)
Path("debug/html").mkdir(parents=True, exist_ok=True)
Path("debug/traces").mkdir(parents=True, exist_ok=True)

logging.basicConfig(
    filename=f"logs/run_{RUN_ID}.log",
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
)

logger = logging.getLogger(__name__)


def parse_args():
    parser = argparse.ArgumentParser(
        description="Scrape peserta paket SPSE dari data-source CSV."
    )

    parser.add_argument(
        "--input-csv",
        required=True,
        help="Path file CSV sumber. Wajib memiliki kolom: Nama Instansi, Kode Paket.",
    )

    parser.add_argument(
        "--output-csv",
        required=True,
        help="Path file CSV hasil scraping peserta.",
    )

    parser.add_argument(
        "--failed-csv",
        required=True,
        help="Path file CSV untuk menyimpan baris yang gagal diproses.",
    )

    parser.add_argument(
        "--year",
        required=True,
        help="Tahun paket yang akan dipilih, contoh: 2025.",
    )

    parser.add_argument(
        "--headless",
        action="store_true",
        help="Jalankan browser dalam mode headless.",
    )

    parser.add_argument(
        "--debug-trace",
        action="store_true",
        help="Aktifkan Playwright trace per row.",
    )

    parser.add_argument(
        "--timeout",
        type=int,
        default=30000,
        help="Timeout Playwright dalam milidetik. Default: 30000.",
    )

    parser.add_argument(
        "--start-row",
        type=int,
        default=1,
        help=(
            "Mulai proses dari row ke-n pada data CSV. "
            "1 berarti baris data pertama setelah header. Default: 1."
        ),
    )

    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Batasi jumlah row yang diproses. Jika tidak diisi, proses sampai akhir CSV.",
    )

    parser.add_argument(
        "--max-consecutive-open-home-failures",
        type=int,
        default=3,
        help=(
            "Hentikan proses jika open_home gagal beruntun sebanyak nilai ini. "
            "Default: 3."
        ),
    )

    return parser.parse_args()


def safe_filename(value: str) -> str:
    return re.sub(r"[^a-zA-Z0-9_-]+", "_", str(value))[:80]


def wait_for_any_selector(page, selectors, timeout=TIMEOUT):
    last_error = None

    for selector in selectors:
        try:
            locator = page.locator(selector).first
            locator.wait_for(state="visible", timeout=timeout)
            return locator, selector
        except Exception as e:
            last_error = e

    raise RuntimeError(
        f"Tidak ada selector yang ditemukan dari kandidat: {selectors}. "
        f"Last error: {last_error}"
    )


def log_page_state(page, row_id, step_name, attempt=None):
    try:
        title = page.title()
    except Exception:
        title = "<failed_get_title>"

    try:
        url = page.url
    except Exception:
        url = "<failed_get_url>"

    logger.warning(
        f"PAGE_STATE row={row_id} step={step_name} attempt={attempt} "
        f"title='{title}' url='{url}'"
    )


def save_debug(page, row_id, kode_paket, step_name):
    debug_name = f"row_{row_id}_{safe_filename(kode_paket)}_{safe_filename(step_name)}"

    try:
        page.screenshot(
            path=f"debug/screenshots/{debug_name}.png",
            full_page=True,
        )
    except Exception as e:
        logger.warning(f"Failed screenshot row={row_id} step={step_name}: {e}")

    try:
        html = page.content()
        Path(f"debug/html/{debug_name}.html").write_text(html, encoding="utf-8")
    except Exception as e:
        logger.warning(f"Failed save HTML row={row_id} step={step_name}: {e}")


def append_dicts_to_csv(path, rows):
    if not rows:
        return

    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)

    new_df = pd.DataFrame(rows)

    if not path.exists():
        new_df.to_csv(path, index=False, encoding="utf-8-sig")
        return

    try:
        old_df = pd.read_csv(path, dtype=str).fillna("")
        combined_df = pd.concat([old_df, new_df], ignore_index=True).fillna("")
        combined_df.to_csv(path, index=False, encoding="utf-8-sig")
    except pd.errors.EmptyDataError:
        new_df.to_csv(path, index=False, encoding="utf-8-sig")


def append_failed_row(
    failed_csv,
    row_id,
    nama_instansi,
    kode_paket,
    failed_step,
    error_message,
):
    failed_row = {
        "row_id": row_id,
        "Nama Instansi": nama_instansi,
        "Kode Paket": kode_paket,
        "failed_step": failed_step,
        "error_message": str(error_message),
    }

    append_dicts_to_csv(failed_csv, [failed_row])


def run_step(row_id, step_name, func):
    logger.info(f"START_STEP row={row_id} step={step_name}")

    try:
        result = func()
        logger.info(f"OK_STEP row={row_id} step={step_name}")
        return result
    except Exception as e:
        logger.exception(f"FAIL_STEP row={row_id} step={step_name}")
        raise e


def open_home(page, row_id=None, max_attempts=3):
    last_error = None

    for attempt in range(1, max_attempts + 1):
        try:
            logger.info(f"OPEN_HOME_ATTEMPT row={row_id} attempt={attempt}")

            page.goto(BASE_URL, wait_until="domcontentloaded", timeout=TIMEOUT)

            try:
                page.wait_for_load_state("networkidle", timeout=10000)
            except Exception:
                logger.warning(
                    f"NETWORKIDLE_TIMEOUT row={row_id} attempt={attempt}; "
                    f"lanjut wait selector"
                )

            search_locator, matched_selector = wait_for_any_selector(
                page,
                SPSE_SEARCH_SELECTORS,
                timeout=TIMEOUT,
            )

            logger.info(
                f"OPEN_HOME_MATCHED_SELECTOR row={row_id} "
                f"attempt={attempt} selector='{matched_selector}'"
            )

            return search_locator

        except Exception as e:
            last_error = e

            logger.exception(
                f"OPEN_HOME_ATTEMPT_FAILED row={row_id} attempt={attempt}"
            )

            log_page_state(page, row_id=row_id, step_name="open_home", attempt=attempt)

            try:
                save_debug(page, row_id, "open_home", f"open_home_attempt_{attempt}")
            except Exception as debug_error:
                logger.warning(
                    f"OPEN_HOME_DEBUG_FAILED row={row_id} "
                    f"attempt={attempt}: {debug_error}"
                )

            if attempt < max_attempts:
                try:
                    page.wait_for_timeout(3000)
                    page.reload(wait_until="domcontentloaded", timeout=TIMEOUT)
                except Exception:
                    pass

    if last_error is not None:
        raise last_error

    raise RuntimeError("open_home gagal tanpa exception yang tertangkap.")


def search_instansi(page, nama_instansi):
    search_box, matched_selector = wait_for_any_selector(
        page,
        SPSE_SEARCH_SELECTORS,
        timeout=TIMEOUT,
    )

    logger.info(
        f"SEARCH_INSTANSI_INPUT_SELECTOR instansi='{nama_instansi}' "
        f"selector='{matched_selector}'"
    )

    search_box.fill(nama_instansi)

    first_result = page.locator(
        ".select2-results__option, .tt-suggestion, li"
    ).first

    first_result.wait_for(timeout=TIMEOUT)
    first_result.click()

    page.wait_for_timeout(500)


def click_masuk(page):
    page.get_by_role("button", name=re.compile("Masuk", re.I)).click()
    page.wait_for_load_state("networkidle")


def open_cari_paket(page):
    page.get_by_role("link", name=re.compile("CARI PAKET", re.I)).click()
    page.wait_for_load_state("networkidle")
    page.wait_for_selector('select[name="tahun"]', timeout=TIMEOUT)


def select_tahun(page, year):
    page.locator('select[name="tahun"]').select_option(str(year))

    try:
        page.wait_for_load_state("networkidle", timeout=10000)
    except Exception:
        logger.warning("NETWORKIDLE_TIMEOUT step=select_tahun; lanjut proses")


def search_kode_paket(page, kode_paket):
    search_input = page.locator('#tbllelang_filter input[type="search"]')
    search_input.wait_for(timeout=TIMEOUT)
    search_input.fill(str(kode_paket))
    search_input.press("Enter")

    try:
        page.wait_for_load_state("networkidle", timeout=10000)
    except Exception:
        logger.warning("NETWORKIDLE_TIMEOUT step=search_kode_paket; lanjut wait table")

    page.wait_for_selector("#tbllelang tbody tr", timeout=TIMEOUT)


def open_detail_paket(page):
    rows = page.locator("#tbllelang tbody tr")
    first_row = rows.first
    first_row.wait_for(timeout=TIMEOUT)

    nama_paket_link = first_row.locator("a").first
    nama_paket_link.wait_for(timeout=TIMEOUT)

    try:
        with page.expect_popup(timeout=TIMEOUT) as popup_info:
            nama_paket_link.click()

        detail_page = popup_info.value
        detail_page.wait_for_load_state("domcontentloaded")

        try:
            detail_page.wait_for_load_state("networkidle", timeout=10000)
        except Exception:
            logger.warning("NETWORKIDLE_TIMEOUT step=open_detail_paket popup")

        logger.info(f"DETAIL_PAGE_OPENED mode='popup' url='{detail_page.url}'")
        return detail_page

    except PlaywrightTimeoutError:
        logger.warning(
            "OPEN_DETAIL_NO_POPUP_DETECTED fallback='same_page_navigation'"
        )

        nama_paket_link.click()
        page.wait_for_load_state("domcontentloaded")

        try:
            page.wait_for_load_state("networkidle", timeout=10000)
        except Exception:
            logger.warning("NETWORKIDLE_TIMEOUT step=open_detail_paket same_page")

        logger.info(f"DETAIL_PAGE_OPENED mode='same_page' url='{page.url}'")
        return page


def open_tab_peserta(page):
    page.get_by_role("link", name=re.compile("Peserta", re.I)).click()

    try:
        page.wait_for_load_state("networkidle", timeout=10000)
    except Exception:
        logger.warning("NETWORKIDLE_TIMEOUT step=open_tab_peserta; lanjut wait table")

    page.wait_for_selector("table tbody tr", timeout=TIMEOUT)


def scrape_peserta_table(page, kode_paket):
    table = page.locator("table").last
    table.wait_for(timeout=TIMEOUT)

    headers = table.locator("thead tr th").all_inner_texts()

    if not headers:
        first_row_cells = table.locator("tbody tr").first.locator("td").count()
        headers = [f"kolom_{i + 1}" for i in range(first_row_cells)]

    rows = []
    body_rows = table.locator("tbody tr")
    total_rows = body_rows.count()

    for i in range(total_rows):
        cells = body_rows.nth(i).locator("td").all_inner_texts()

        if not cells:
            continue

        row_data = {}

        for idx, cell in enumerate(cells):
            column_name = headers[idx] if idx < len(headers) else f"kolom_{idx + 1}"
            row_data[column_name.strip()] = cell.strip()

        row_data["Kode Paket"] = kode_paket
        rows.append(row_data)

    return rows


def process_row(
    context,
    row_id,
    nama_instansi,
    kode_paket,
    output_csv,
    failed_csv,
    year,
    debug_trace=False,
):
    page = context.new_page()
    page.set_default_timeout(TIMEOUT)

    detail_page = None
    active_page = page

    trace_path = f"debug/traces/row_{row_id}_{safe_filename(kode_paket)}.zip"

    if debug_trace:
        context.tracing.start(
            screenshots=True,
            snapshots=True,
            sources=True,
        )

    failed_step = None

    try:
        failed_step = "open_home"
        run_step(row_id, failed_step, lambda: open_home(page, row_id=row_id))

        failed_step = "search_instansi"
        run_step(row_id, failed_step, lambda: search_instansi(page, nama_instansi))

        failed_step = "click_masuk"
        run_step(row_id, failed_step, lambda: click_masuk(page))

        failed_step = "open_cari_paket"
        run_step(row_id, failed_step, lambda: open_cari_paket(page))

        failed_step = "select_tahun"
        run_step(row_id, failed_step, lambda: select_tahun(page, year))

        failed_step = "search_kode_paket"
        run_step(row_id, failed_step, lambda: search_kode_paket(page, kode_paket))

        failed_step = "open_detail_paket"
        detail_page = run_step(row_id, failed_step, lambda: open_detail_paket(page))
        detail_page.set_default_timeout(TIMEOUT)
        active_page = detail_page

        failed_step = "open_tab_peserta"
        run_step(row_id, failed_step, lambda: open_tab_peserta(detail_page))

        failed_step = "scrape_peserta_table"
        peserta_rows = run_step(
            row_id,
            failed_step,
            lambda: scrape_peserta_table(detail_page, kode_paket),
        )

        if peserta_rows:
            append_dicts_to_csv(output_csv, peserta_rows)
        else:
            logger.warning(
                f"EMPTY_RESULT row={row_id} instansi='{nama_instansi}' "
                f"kode_paket='{kode_paket}'"
            )

        logger.info(
            f"SUCCESS row={row_id} instansi='{nama_instansi}' "
            f"kode_paket='{kode_paket}' peserta_rows={len(peserta_rows)}"
        )

        return {
            "success": True,
            "failed_step": None,
            "peserta_rows": len(peserta_rows),
        }

    except Exception as e:
        save_debug(active_page, row_id, kode_paket, failed_step)

        append_failed_row(
            failed_csv=failed_csv,
            row_id=row_id,
            nama_instansi=nama_instansi,
            kode_paket=kode_paket,
            failed_step=failed_step,
            error_message=e,
        )

        logger.exception(
            f"FAILED row={row_id} instansi='{nama_instansi}' "
            f"kode_paket='{kode_paket}' step='{failed_step}'"
        )

        return {
            "success": False,
            "failed_step": failed_step,
            "peserta_rows": 0,
        }

    finally:
        if debug_trace:
            try:
                context.tracing.stop(path=trace_path)
            except Exception as e:
                logger.warning(f"Failed stop trace row={row_id}: {e}")

        try:
            if detail_page and detail_page != page and not detail_page.is_closed():
                detail_page.close()
        except Exception as e:
            logger.warning(f"Failed close detail_page row={row_id}: {e}")

        try:
            if page and not page.is_closed():
                page.close()
        except Exception as e:
            logger.warning(f"Failed close page row={row_id}: {e}")


def main():
    global TIMEOUT

    args = parse_args()
    start_time = time.perf_counter()

    input_csv = args.input_csv
    output_csv = args.output_csv
    failed_csv = args.failed_csv
    year = args.year
    headless = args.headless
    debug_trace = args.debug_trace
    TIMEOUT = args.timeout
    start_row = args.start_row
    limit = args.limit
    max_consecutive_open_home_failures = args.max_consecutive_open_home_failures

    Path(output_csv).parent.mkdir(parents=True, exist_ok=True)
    Path(failed_csv).parent.mkdir(parents=True, exist_ok=True)

    df = pd.read_csv(input_csv, dtype=str).fillna("")

    required_columns = ["Nama Instansi", "Kode Paket"]
    for col in required_columns:
        if col not in df.columns:
            raise ValueError(f"Kolom wajib tidak ditemukan: {col}")

    if start_row < 1:
        raise ValueError("--start-row harus bernilai minimal 1.")

    if limit is not None and limit < 1:
        raise ValueError("--limit harus bernilai minimal 1 jika diisi.")

    start_index = start_row - 1

    if start_index >= len(df):
        raise ValueError(
            f"--start-row {start_row} melebihi jumlah baris data CSV: {len(df)}"
        )

    if limit is None:
        df_to_process = df.iloc[start_index:].copy()
    else:
        df_to_process = df.iloc[start_index:start_index + limit].copy()

    total_rows_to_process = len(df_to_process)

    logger.info(
        f"START_RUN input_csv='{input_csv}' output_csv='{output_csv}' "
        f"failed_csv='{failed_csv}' year='{year}' headless={headless} "
        f"debug_trace={debug_trace} timeout={TIMEOUT} "
        f"start_row={start_row} limit={limit} "
        f"total_rows_to_process={total_rows_to_process}"
    )

    processed_count = 0
    success_count = 0
    failed_count = 0
    total_peserta_rows = 0
    consecutive_open_home_failures = 0

    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=headless,
            args=[
                "--disable-blink-features=AutomationControlled",
                "--disable-dev-shm-usage",
                "--no-sandbox",
            ],
        )

        context = browser.new_context(
            viewport={"width": 1366, "height": 768},
            accept_downloads=True,
            locale="id-ID",
            timezone_id="Asia/Jakarta",
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
        )

        context.add_init_script(
            """
            Object.defineProperty(navigator, 'webdriver', {
                get: () => undefined
            });
            """
        )

        for idx, row in df_to_process.iterrows():
            row_id = idx + 1
            nama_instansi = row["Nama Instansi"].strip()
            kode_paket = row["Kode Paket"].strip()

            processed_count += 1

            if not nama_instansi or not kode_paket:
                append_failed_row(
                    failed_csv=failed_csv,
                    row_id=row_id,
                    nama_instansi=nama_instansi,
                    kode_paket=kode_paket,
                    failed_step="validate_input",
                    error_message="Nama Instansi atau Kode Paket kosong.",
                )
                failed_count += 1
                continue

            logger.info(
                f"START_ROW row={row_id} "
                f"instansi='{nama_instansi}' kode_paket='{kode_paket}' year='{year}'"
            )

            result = process_row(
                context=context,
                row_id=row_id,
                nama_instansi=nama_instansi,
                kode_paket=kode_paket,
                output_csv=output_csv,
                failed_csv=failed_csv,
                year=year,
                debug_trace=debug_trace,
            )

            if result and result.get("success"):
                success_count += 1
                total_peserta_rows += int(result.get("peserta_rows", 0) or 0)
            else:
                failed_count += 1

            if result and result.get("failed_step") == "open_home":
                consecutive_open_home_failures += 1
            else:
                consecutive_open_home_failures = 0

            if consecutive_open_home_failures >= max_consecutive_open_home_failures:
                logger.error(
                    f"STOP_RUN reason='open_home_failed_consecutively' "
                    f"count={consecutive_open_home_failures}"
                )
                print(
                    f"Proses dihentikan karena open_home gagal "
                    f"{consecutive_open_home_failures} kali beruntun."
                )
                break

        context.close()
        browser.close()

    elapsed = time.perf_counter() - start_time
    avg_per_row = elapsed / processed_count if processed_count else 0

    logger.info(
        f"END_RUN elapsed={elapsed:.2f}s "
        f"processed_count={processed_count} "
        f"success_count={success_count} "
        f"failed_count={failed_count} "
        f"total_peserta_rows={total_peserta_rows} "
        f"avg_per_row={avg_per_row:.2f}s"
    )

    print(f"Selesai. Output tersimpan di: {output_csv}")
    print(f"Failed rows tersimpan di: {failed_csv}")
    print(f"Total row diproses: {processed_count}")
    print(f"Total row sukses: {success_count}")
    print(f"Total row gagal: {failed_count}")
    print(f"Total baris peserta discrape: {total_peserta_rows}")
    print(f"Total waktu: {elapsed:.2f} detik")
    print(f"Rata-rata per URL: {avg_per_row:.2f} detik")


if __name__ == "__main__":
    main()