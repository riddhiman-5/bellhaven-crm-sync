"""Scrape every Bellhaven location from the website.

Walks the paginated directory AND the homepage/About page, because one community
(Bellhaven Meadows of Findlay) is only linked from the homepage, not the directory.
"""
import re
import time
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

from config import BASE, USER_AGENT

DETAIL_RE = re.compile(r"^/communities/[^/?#]+/?$")
HEADERS = {"User-Agent": USER_AGENT}


def _soup(url):
    r = requests.get(url, headers=HEADERS, timeout=30)
    r.raise_for_status()
    return BeautifulSoup(r.text, "html.parser")


def _detail_links(soup, page_url):
    links = set()
    for a in soup.find_all("a", href=True):
        path = urlparse(urljoin(page_url, a["href"])).path
        if DETAIL_RE.match(path):
            links.add(BASE + path.rstrip("/"))
    return links


def parse_detail(url):
    soup = _soup(url)
    fields = {}
    for dt in soup.select("dl.detail dt"):
        dd = dt.find_next_sibling("dd")
        fields[dt.get_text(strip=True).lower()] = dd
    addr_lines = list(fields["address"].stripped_strings) if "address" in fields else []
    street = city = state = zip_ = None
    for i, line in enumerate(addr_lines):
        m = re.match(r"^(.*?),\s*([A-Z]{2})\s+(\d{5}(?:-\d{4})?)$", line)
        if m:
            city, state, zip_ = m.group(1).strip(), m.group(2), m.group(3)
            street = ", ".join(addr_lines[:i])
    offerings = [b.get_text(strip=True) for b in fields["care offerings"].select(".badge")] if "care offerings" in fields else []
    h1 = soup.find("h1")
    return {
        "url": url,
        "slug": url.rstrip("/").split("/")[-1],
        "name": h1.get_text(strip=True) if h1 else None,
        "street": street, "city": city, "state": state, "zip": zip_,
        "care_offerings": offerings,
        "administrator": fields["administrator"].get_text(strip=True) if "administrator" in fields else None,
        "phone": fields["phone"].get_text(strip=True) if "phone" in fields else None,
    }


def scrape():
    found = {}
    page = 1
    while page <= 30:
        url = f"{BASE}/communities?page={page}"
        new = _detail_links(_soup(url), url) - set(found)
        if not new:
            break
        for link in new:
            found[link] = f"directory page {page}"
        page += 1
        time.sleep(0.1)
    for extra in ("/", "/about"):
        for link in _detail_links(_soup(BASE + extra), BASE + extra):
            found.setdefault(link, f"linked from {extra} only (not in directory)")
    locations = []
    for url in sorted(found):
        loc = parse_detail(url)
        loc["found_on"] = found[url]
        if not loc["zip"]:
            raise RuntimeError(f"Could not parse address on {url} - site layout may have changed")
        locations.append(loc)
        time.sleep(0.1)
    return locations
