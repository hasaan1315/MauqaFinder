"""
ppsc_probe.py - ONE-TIME helper for the PPSC jobs page (https://www.ppsc.gop.pk/Jobs.aspx)

What it does, in this order:
  1. Downloads robots.txt and checks whether automated access to /Jobs.aspx is allowed.
     If it is NOT allowed, it STOPS here and does not download the page.
  2. If allowed: downloads the jobs list page once and saves the HTML.
  3. Writes a short report about the page (ASP.NET postback paging, tables, links, PDFs).

Run:   python ppsc_probe.py
Then send me: ppsc_robots.txt, ppsc_list.html and ppsc_probe_report.txt

It makes 2 requests (plus the automatic redirect to the session address).

NOTE: do not copy the long "(S(...))" part from your browser address bar. That is a temporary
session id that expires. The script starts from https://www.ppsc.gop.pk/Jobs.aspx and the site
hands out a fresh session by itself.
"""
import re
import sys
from urllib import robotparser
from urllib.parse import urljoin

import requests

SITE = (sys.argv[1] if len(sys.argv) > 1 else "https://www.ppsc.gop.pk").rstrip("/")
START = SITE + "/Jobs.aspx"
USER_AGENT = "Mozilla/5.0 (compatible; MaukaFinderBot/0.1; job-matching research project)"
HEADERS = {"User-Agent": USER_AGENT, "Accept": "text/html,application/xhtml+xml,*/*;q=0.8"}


def stop(msg: str, code: int = 2):
    print("\n" + msg)
    sys.exit(code)


# ---------------------------------------------------------------- 1. robots.txt
print("Step 1: robots.txt")
try:
    r = requests.get(SITE + "/robots.txt", headers=HEADERS, timeout=30)
except requests.RequestException as e:
    stop(f"Could not download robots.txt ({type(e).__name__}). Try again later. Nothing else was requested.", 1)

with open("ppsc_robots.txt", "w", encoding="utf-8") as f:
    f.write(r.text)
print(f"  robots.txt: HTTP {r.status_code}, {len(r.text)} characters -> ppsc_robots.txt")

rp = robotparser.RobotFileParser()
if r.status_code == 200:
    rp.parse(r.text.splitlines())
    allowed = rp.can_fetch(USER_AGENT, START) and rp.can_fetch("*", START)
    delay = rp.crawl_delay("*") or rp.crawl_delay(USER_AGENT)
elif r.status_code in (401, 403):
    allowed, delay = False, None                      # access to robots.txt itself is forbidden: treat as "no"
elif r.status_code == 404:
    allowed, delay = True, None                       # no robots.txt = no restrictions stated
else:
    stop(f"Unexpected answer for robots.txt (HTTP {r.status_code}). Not continuing.", 1)

shown = [ln for ln in r.text.splitlines() if ln.strip()][:25]
if shown:
    print("  first lines:")
    for ln in shown:
        print("    " + ln)
print("  crawl-delay:", delay if delay else "none stated")

if not allowed:
    stop("RESULT: robots.txt does NOT allow automated access to /Jobs.aspx.\n"
         "The page was NOT downloaded, and this project should not scrape it.\n"
         "Options: ask PPSC for permission or for a data feed, or check whether the same posts already\n"
         "appear on the other two portals. Send me ppsc_robots.txt and we will decide together.")
print("  RESULT: allowed.\n")

# ---------------------------------------------------------------- 2. the list page
print("Step 2: jobs list page")
session = requests.Session()
session.headers.update(HEADERS)
try:
    page = session.get(START, timeout=30, allow_redirects=True)
except requests.RequestException as e:
    stop(f"Could not download the jobs page ({type(e).__name__}). Try again later.", 1)

html = page.text
with open("ppsc_list.html", "w", encoding="utf-8") as f:
    f.write(html)
print(f"  HTTP {page.status_code}, {len(html):,} characters, final address: {page.url}")

# ---------------------------------------------------------------- 3. report
postbacks = re.findall(r"__doPostBack\('([^']*)'", html)
links = re.findall(r'<a\b[^>]*href="([^"]+)"', html, re.I)
report = [
    f"final address            : {page.url}",
    f"session id in the address: {'yes' if '(S(' in page.url else 'no'}",
    f"__VIEWSTATE present      : {'yes' if '__VIEWSTATE' in html else 'no'}",
    f"__EVENTVALIDATION present: {'yes' if '__EVENTVALIDATION' in html else 'no'}",
    f"__doPostBack calls       : {len(postbacks)}  (paging/detail links that need a form post)",
    f"tables / rows            : {len(re.findall(r'<table', html, re.I))} / {len(re.findall(r'<tr', html, re.I))}",
    f"links                    : {len(links)} (pdf: {sum(1 for h in links if h.lower().endswith('.pdf'))})",
    f"images (maybe adverts)   : {len(re.findall(r'<img', html, re.I))}",
    "",
    "first 20 links:",
] + ["  " + urljoin(page.url, h) for h in links[:20]] + ["", "first 10 postback targets:"] + ["  " + p for p in postbacks[:10]]

with open("ppsc_probe_report.txt", "w", encoding="utf-8") as f:
    f.write("\n".join(report))
print("\n".join("  " + ln for ln in report[:8]))
print("\nDone. Send me: ppsc_robots.txt, ppsc_list.html, ppsc_probe_report.txt")
