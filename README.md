# INAProc Playwright Scraper

This scraper opens the INAProc Streamlit page, changes `Entri per halaman` to 50, waits for `table.dataframe`, extracts the table headers and rows, keeps links found inside cells, and can click the `Berikutnya` button for pagination.

## Setup

```powershell
pip install -r requirements.txt
playwright install chromium
```

## Run

Scrape the first page:

```powershell
python inaproc_playwright_scraper.py
```

Scrape 10 pages:

```powershell
python inaproc_playwright_scraper.py --max-pages 10 --output output/inaproc_2025_tender.csv
```

Use a different entry count, or leave the page default unchanged:

```powershell
python inaproc_playwright_scraper.py --entries-per-page 25
python inaproc_playwright_scraper.py --entries-per-page 0
```

Scrape as JSON:

```powershell
python inaproc_playwright_scraper.py --max-pages 3 --format json --output output/inaproc_2025_tender.json
```

Show the browser while scraping:

```powershell
python inaproc_playwright_scraper.py --headful --max-pages 2
```

The default URL is:

```text
https://data.inaproc.id/realisasi?tahun=2025&jenis_klpd=3&jenis_klpd=4&jenis_klpd=5&sumber=Tender
```

## Notes

- The live page may start at 10 rows per page. The scraper changes it to 50 by default.
- The sample HTML shows 50 rows per page and `Halaman 1 dari 719`.
- Use `--max-pages 719` only if you really want the full visible pagination result.
- Add a small `--delay` if the site becomes slow or unstable.
