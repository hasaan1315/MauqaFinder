"""
njp_probe.py - ONE-TIME helper. Saves a few NJP pages so we can see the real HTML.
Run:  python njp_probe.py
Then drag these files into the chat:
  njp_list.html  njp_list_p2.html  njp_detail.html  njp_robots.txt
It makes only 4 requests.
"""
import re, time
import requests

HEADERS = {"User-Agent": "Mozilla/5.0"}


def get(url, out):
    r = requests.get(url, headers=HEADERS, timeout=30)
    print(r.status_code, f"{len(r.text):>7} chars", url, "->", out)
    with open(out, "w", encoding="utf-8") as f:
        f.write(r.text)
    time.sleep(1.5)
    return r.text


lst = get("https://njp.gov.pk/jobs/live", "njp_list.html")
get("https://njp.gov.pk/jobs/live?page=2", "njp_list_p2.html")
get("https://njp.gov.pk/jobs/9692", "njp_detail.html")
get("https://njp.gov.pk/robots.txt", "njp_robots.txt")

print("\nDistinct job links on page 1:", len(set(re.findall(r"/jobs/(\d+)", lst))))
print("Page 1 links to page 2:", "page=2" in lst)
