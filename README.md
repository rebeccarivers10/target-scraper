# Target Sponsored Ad Scraper

An internal tool for identifying brands advertising on Target.com and collecting their contact information for outreach purposes.

---

## Purpose

This tool was built to support competitive research and new business prospecting. It searches Target.com for sponsored (paid) ads under any keyword, identifies the advertising brands, finds their websites, and extracts publicly available contact information (emails and phone numbers).

**Use case:** Sales and business development teams can run keyword searches relevant to their categories, export a list of brands actively spending on Target ads, and reach out to them directly.

---

## What It Does

1. **Searches Target.com** for a given keyword and identifies sponsored product listings
2. **Extracts brand information** — brand name, product title, price, and product URL
3. **Resolves brand websites** using two methods:
   - Checks Target's seller profile pages for contact email domains
   - Falls back to domain pattern guessing (e.g. `brandname.com`) verified with HTTP requests
4. **Scrapes contact info** from the brand's own website — emails and phone numbers from homepage, contact, and about pages
5. **Exports results** to CSV for use in outreach or reporting

---

## How to Run

### Prerequisites

- Python 3.11+
- Install dependencies:
  ```
  pip install -r requirements.txt
  playwright install chromium
  ```

### Web UI (recommended)

```
python app.py
```

Then open **http://localhost:5000** in your browser.

**Single search:** Enter a keyword and click Search. Results appear in a table with brand, website, emails, and phone numbers. Download as CSV.

**Batch mode:** Paste a list of keywords (one per line) and click Start. The tool processes each keyword sequentially and saves results as it goes. Results persist between sessions and can be downloaded at any time.

### Command Line

```
python scraper.py --term "flat iron"
python scraper.py --term "flat iron" --output results.csv
```

---

## Output Fields

| Field | Description |
|---|---|
| search_term | The keyword that was searched |
| brand | Brand name as listed on Target |
| website | Brand's own website domain |
| emails | Contact emails found on the brand's website |
| phones | Phone numbers found on the brand's website |

---

## Files

| File | Description |
|---|---|
| `app.py` | Flask web server — runs the UI and API |
| `scraper.py` | Core scraping logic — Target search, brand website lookup, contact extraction |
| `contact_scraper.py` | Alternate contact extraction module using Target brand slugs |
| `keywords.txt` | Sample keyword list for batch runs |
| `requirements.txt` | Python dependencies |
| `templates/` | HTML templates for the web UI |
| `batch_results.json` | Auto-saved results from the last batch run (not committed to git) |

---

## Data & Privacy

- All data collected is **publicly available** information from Target.com and brand websites
- No login credentials, accounts, or private data are accessed
- Contact information (emails, phones) is scraped only from publicly visible pages on brand-owned websites
- Results are stored locally and not transmitted to any third party

---

## Repository

`github.com/rebeccarivers10/target-scraper`
