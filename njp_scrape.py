"""
njp_scrape.py - STEP 2 (check stage, no database yet)

Reads njp_links.json, downloads every job page (saved in njp_pages/, never
downloaded twice), parses the fields and writes njp_jobs.json so you can CHECK
the data before it goes to Supabase.

Run:  python njp_scrape.py
A dropped connection is retried 2 times (10 seconds apart).
Safe to run again: pages already saved are reused. Delete a file in njp_pages/
(or the whole folder) to download it again.
"""
import json, os, re, sys, time
from datetime import datetime

import requests
from bs4 import BeautifulSoup

LINKS_FILE = "njp_links.json"
PAGES_DIR = "njp_pages"
OUT_FILE = "njp_jobs.json"
HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}
DELAY = 3                                    # seconds between page downloads (be gentle)
RETRIES = 2                                  # retries per page (after the first try)
RETRY_WAIT = 10                              # seconds to wait before each retry

session = requests.Session()
session.headers.update(HEADERS)


# ------------------------------------------------------------------ download
def get_html(url):
    """Download one page. Retries (RETRIES times) when the site drops the connection. None = gave up."""
    for attempt in range(RETRIES + 1):
        try:
            r = session.get(url, timeout=30)
            r.raise_for_status()
            return r.text
        except requests.RequestException as e:
            print(f"  failed: {type(e).__name__}")
            if attempt < RETRIES:
                print(f"  retry {attempt + 1}/{RETRIES} in {RETRY_WAIT}s...")
                time.sleep(RETRY_WAIT)
    return None


# ------------------------------------------------------------------- parsing
def norm(s):
    return re.sub(r"\s+", " ", s).rstrip(":").strip().lower()


LABELS = {"grade", "job type", "vacancies", "job description", "eligibility criteria",
          "qualifications", "experience", "age limit", "application deadline",
          "quick overview", "about employer", "read more"}


def val(lines, label):
    """The line right after a label line (None if missing or if it is another label)."""
    for i, l in enumerate(lines):
        if norm(l) == label and i + 1 < len(lines):
            nxt = lines[i + 1].strip()
            return None if (not nxt or norm(nxt) in LABELS) else nxt
    return None


DATE = r"(\d{1,2}\s+[A-Za-z]{3}\s+\d{4})"            # 09 Oct 2026


def to_iso(s):
    try:
        return datetime.strptime(re.sub(r"\s+", " ", s), "%d %b %Y").date().isoformat()
    except Exception:
        return None


def find_date(text, label):
    m = re.search(label + r":?\s*" + DATE, text)
    return to_iso(m.group(1)) if m else None


def get_description(lines):
    start = next((i for i, l in enumerate(lines) if norm(l) == "job description"), None)
    if start is None:
        return None
    out = []
    for l in lines[start + 1:]:
        if norm(l) == "eligibility criteria":
            break
        if norm(l) != "read more":
            out.append(l.strip())
    return "\n".join(out).strip() or None


def get_qualifications(lines):
    """Degree names listed under Eligibility Criteria > Qualifications."""
    elig = next((i for i, l in enumerate(lines) if norm(l) == "eligibility criteria"), None)
    if elig is None:
        return []
    start = next((i for i in range(elig, len(lines)) if norm(lines[i]) == "qualifications"), None)
    if start is None:
        return []
    out = []
    for l in lines[start + 1:]:
        if norm(l) in {"read more", "experience", "age limit", "application deadline"}:
            break
        if l.strip():
            out.append(l.strip())
    return out


def parse_job(job_id, list_title, html):
    soup = BeautifulSoup(html, "lxml")
    h1 = soup.find("h1")
    for t in soup(["script", "style"]):
        t.decompose()
    lines = [l for l in soup.get_text("\n", strip=True).split("\n") if l.strip()]
    flat = " ".join(lines)

    title = h1.get_text(" ", strip=True) if h1 else list_title

    employer = None
    if title in lines:                                       # line after the title = employer
        i = lines.index(title)
        if i + 1 < len(lines) and norm(lines[i + 1]) not in ("government of pakistan", "grade"):
            employer = lines[i + 1]

    # "Quick Overview" block: Posted / Deadline / Vacancies / Experience / Age Limit
    qo = ""
    if "Quick Overview" in flat:
        qo = flat.split("Quick Overview", 1)[1].split("About Employer", 1)[0]

    vac = re.search(r"Vacancies:?\s*(\d+)", qo) or re.search(r"(\d+)\s*vacanc", flat, re.I)

    exp = None
    m = re.search(r"Experience:?\s*(?:(\d+)\s*\+?\s*years?|(no\s*experience))", qo, re.I)
    if m:
        exp = int(m.group(1)) if m.group(1) else 0

    age_min = age_max = None
    if "Age Limit" in qo:
        part = qo.split("Age Limit", 1)[1]
        mn = re.search(r"Min:?\s*(\d+)", part, re.I)
        mx = re.search(r"Max:?\s*(\d+)", part, re.I)
        age_min = int(mn.group(1)) if mn else None
        age_max = int(mx.group(1)) if mx else None
        if not mn and not mx:
            n = re.search(r"(\d+)", part)
            age_max = int(n.group(1)) if n else None

    deadline = find_date(qo, "Deadline")
    if deadline is None:                                     # fallback: "Application Deadline" box
        d = val(lines, "application deadline")
        m = re.search(DATE, d or "")
        deadline = to_iso(m.group(1)) if m else None

    return {
        "id": job_id,
        "title": title,
        "employer": employer,
        "job_type": val(lines, "job type"),
        "grade": val(lines, "grade"),
        "vacancies": int(vac.group(1)) if vac else None,
        "experience_years": exp,
        "age_min": age_min,
        "age_max": age_max,
        "qualifications": get_qualifications(lines),
        "description": get_description(lines),
        "job_posted": find_date(qo, "Posted"),
        "last_date_to_apply": deadline,
        "url": f"https://njp.gov.pk/jobs/{job_id}",
    }


# ---------------------------------------------------------------------- main
def main():
    if not os.path.exists(LINKS_FILE):
        sys.exit(f"{LINKS_FILE} not found. Run njp_links.py first.")
    with open(LINKS_FILE, encoding="utf-8") as f:
        links = json.load(f)
    os.makedirs(PAGES_DIR, exist_ok=True)

    jobs, failed = [], []
    for n, item in enumerate(links, 1):
        job_id = item["id"]
        path = os.path.join(PAGES_DIR, job_id + ".html")
        if os.path.exists(path):
            with open(path, encoding="utf-8") as f:
                html = f.read()
        else:
            print(f"[{n}/{len(links)}] downloading {job_id}")
            html = get_html(item["url"])
            if html is None:
                print("  FAILED, skipped (run again later to retry)")
                failed.append(job_id)
                continue
            with open(path, "w", encoding="utf-8") as f:
                f.write(html)
            time.sleep(DELAY)
        jobs.append(parse_job(job_id, item.get("title"), html))

    with open(OUT_FILE, "w", encoding="utf-8") as f:
        json.dump(jobs, f, ensure_ascii=False, indent=2)

    print(f"\nParsed: {len(jobs)} of {len(links)} jobs | saved -> {OUT_FILE}")
    if failed:
        print("Failed downloads:", failed)
    if jobs:
        print("\nField         filled")
        for k in jobs[0]:
            n = sum(1 for j in jobs if j[k] not in (None, [], ""))
            print(f"{k:22}{n}/{len(jobs)}")


if __name__ == "__main__":
    main()