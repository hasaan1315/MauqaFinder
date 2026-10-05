"""
njp_links.py - collect the title + link of EVERY job on https://njp.gov.pk/jobs/live
(all pages) and save them to njp_links.json.

Run:  python njp_links.py

If the site drops a connection, the page is retried (5 tries, waiting longer each time).
If a page still fails, NOTHING is saved: a partial list would be wrong.
"""
import json, re, sys, time
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

BASE = "https://njp.gov.pk/jobs/live"
HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}
DELAY = 3                                    # seconds between pages (be gentle)
TRIES = 5                                    # attempts per page
MAX_PAGES = 50                               # safety limit
OUT_FILE = "njp_links.json"

JOB_HREF = re.compile(r"/jobs/(\d+)/?$")     # .../jobs/9692
NOT_A_TITLE = {"view details", "login to apply", "login to apply →", "apply"}
TOTAL_RE = re.compile(r"of\s+(\d+)\s+(?:jobs|results)", re.I)   # "Showing 1 to 20 of 59 jobs"

session = requests.Session()
session.headers.update(HEADERS)


def get_page(page):
    """Download one list page. Retries when the site drops the connection."""
    for attempt in range(1, TRIES + 1):
        try:
            r = session.get(BASE, params={"page": page} if page > 1 else None, timeout=30)
            r.raise_for_status()
            return r.text
        except requests.RequestException as e:
            print(f"  page {page}, try {attempt}/{TRIES} failed: {type(e).__name__}")
            if attempt == TRIES:
                sys.exit(f"Page {page} failed {TRIES} times. Nothing saved. Try again later.")
            wait = 5 * attempt                              # 5s, 10s, 15s, 20s
            print(f"  waiting {wait}s, then retrying...")
            time.sleep(wait)


jobs = {}                                    # id -> {"id", "title", "url"}
expected = None

for page in range(1, MAX_PAGES + 1):
    soup = BeautifulSoup(get_page(page), "lxml")

    if expected is None:
        m = TOTAL_RE.search(soup.get_text(" ", strip=True))
        expected = int(m.group(1)) if m else None

    new = 0
    for a in soup.find_all("a", href=JOB_HREF):
        job_id = JOB_HREF.search(a["href"]).group(1)
        if job_id not in jobs:
            jobs[job_id] = {"id": job_id, "title": None,
                            "url": urljoin(BASE, f"/jobs/{job_id}")}
            new += 1
        text = a.get_text(" ", strip=True)           # the title link has the title as text,
        if text and text.lower() not in NOT_A_TITLE and not jobs[job_id]["title"]:
            jobs[job_id]["title"] = text             # "View Details" links are skipped

    print(f"page {page}: {new} new jobs")
    if new == 0:                                     # nothing new -> we passed the last page
        break
    time.sleep(DELAY)

result = list(jobs.values())
with open(OUT_FILE, "w", encoding="utf-8") as f:
    json.dump(result, f, ensure_ascii=False, indent=2)

print(f"\nTotal jobs: {len(result)} | site says: {expected} | saved -> {OUT_FILE}")
no_title = [j["id"] for j in result if not j["title"]]
if no_title:
    print("WARNING: no title found for", no_title)
if expected is not None and expected != len(result):
    print("WARNING: count differs from what the site says. Paste this output here.")
for j in result[:3]:
    print(j)
