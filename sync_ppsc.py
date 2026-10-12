"""
sync_ppsc.py - keep the Supabase table raw.ppsc_jobs in sync with PPSC active jobs list.
"""
import os
import re
import sys
from datetime import datetime

import requests
from bs4 import BeautifulSoup

import sync_pjp as base  # shared Supabase database helpers

LIST_URL = "https://www.ppsc.gop.pk/Jobs.aspx"
TABLE = "ppsc_jobs"
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36",
}

session = requests.Session()
session.headers.update(HEADERS)


def fetch_list():
    """Fetch and parse the 8-column PPSC live jobs table."""
    print("Fetching PPSC Jobs Table...")
    try:
        res = session.get(LIST_URL, timeout=30)
        res.raise_for_status()
    except Exception as e:
        sys.exit(f"Failed to fetch PPSC page: {e}")

    soup = BeautifulSoup(res.text, 'lxml')
    live = {}

    rows = soup.select("table tr")
    for row in rows:
        cols = row.find_all("td")
        
        # Ensure it's a full 8-column data row
        if len(cols) < 8:
            continue
            
        case_no = cols[2].get_text(strip=True)
        title = cols[3].get_text(strip=True)
        fee_text = cols[4].get_text(strip=True)
        department = cols[5].get_text(strip=True)
        closing_text = cols[7].get_text(strip=True)
        
        if not case_no or not title:
            continue
            
        # 1. Clean ID
        record_id = re.sub(r'[^a-zA-Z0-9]', '_', case_no).lower()
        
        # 2. Parse Date
        closing_date = None
        try:
            closing_date = datetime.strptime(closing_text, "%d-%m-%Y").date().isoformat()
        except ValueError:
            pass

        # 3. Parse Fee
        fee = 600
        fee_match = re.search(r'\d+', fee_text)
        if fee_match:
            fee = int(fee_match.group())

        # 4. Extract Grade from Title if present (e.g., "ASSISTANT DIRECTOR (BS-17)")
        grade_match = re.search(r'(BS-\d+|BPS-\d+)', title, re.I)
        grade = grade_match.group(1).upper() if grade_match else None

        live[record_id] = {
            "id": record_id,
            "title": title,
            "department": department,
            "grade": grade,
            "vacancies": 1,  # Not present on list page
            "description": f"{title} at {department}. Case No: {case_no}",
            "qualification_text": None, # Requires scraping the "Apply" portal
            "eligibility": None,        # Requires scraping the "Apply" portal
            "place_of_posting": "Punjab",
            "fee": fee,
            "closing_date": closing_date,
            "url": LIST_URL,
            "is_active": True
        }

    return live


def main(force=False):
    live = fetch_list()
    print("Listed on PPSC site:", len(live))
    if not live:
        sys.exit("No jobs found on PPSC site. Stopping.")

    db = base.get_db()
    try:
        saved = base.db_read(db, "id,is_active", tbl=TABLE)
    except Exception as e:
        sys.exit(f"Supabase connection error for {TABLE}: {e}")

    known = {r["id"]: r["is_active"] for r in saved}
    active = {i for i, k in known.items() if k}

    new = [s for s in live if s not in known]
    back = [s for s in live if s in known and not known[s]]
    gone = [s for s in active if s not in live]

    if active and len(gone) > max(3, len(active) // 2) and not force:
        sys.exit(f"Would remove {len(gone)} PPSC jobs. Run with --force if intentional.")

    rows = [live[job_id] for job_id in list(live)]
    
    try:
        base.db_upsert(db, rows, tbl=TABLE)
        base.db_retire(db, gone, tbl=TABLE)
    except Exception as e:
        sys.exit(f"Supabase write FAILED: {e}")

    print(f"New: {len(new)} | Reactivated: {len(back)} | Removed: {len(gone)}")


if __name__ == "__main__":
    main(force="--force" in sys.argv)