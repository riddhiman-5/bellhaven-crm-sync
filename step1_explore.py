"""
STEP 1 - EXPLORE (read-only, changes nothing in the CRM)

What this does:
  1. Figures out how the API wants the token sent (tries common formats on /me).
  2. Downloads every CRM account and contact  -> data/crm_accounts.json, data/crm_contacts.json
  3. Scrapes every Bellhaven location from the website (directory pages + homepage)
     and opens each detail page for address/zip/admin/phone -> data/website_locations.json

How to run (in Terminal, inside this folder):
  pip3 install requests beautifulsoup4
  python3 step1_explore.py
It will ask you to paste your API token.
"""

import json
import os
import re
import sys
import time
from getpass import getpass
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

BASE = "https://analyst-assessment-production.up.railway.app"
API = BASE + "/api/v1"
DATA_DIR = "data"
HEADERS_COMMON = {"User-Agent": "bellhaven-crm-sync/0.1"}

os.makedirs(DATA_DIR, exist_ok=True)


def save(name, obj):
    path = os.path.join(DATA_DIR, name)
    with open(path, "w") as f:
        json.dump(obj, f, indent=2)
    print(f"  saved {path}")


# ---------------------------------------------------------------- 1. auth
def detect_auth(token):
    """Try common ways of sending a token; return a dict of headers/params that works."""
    candidates = [
        ("Authorization: Bearer", {"Authorization": f"Bearer {token}"}, {}),
        ("X-API-Key", {"X-API-Key": token}, {}),
        ("X-API-Token", {"X-API-Token": token}, {}),
        ("X-Token", {"X-Token": token}, {}),
        ("Authorization: Token", {"Authorization": f"Token {token}"}, {}),
        ("Authorization: raw", {"Authorization": token}, {}),
        ("?token= query", {}, {"token": token}),
        ("?api_key= query", {}, {"api_key": token}),
    ]
    print("\n[1] Detecting how to send the token...")
    first_error = None
    for label, headers, params in candidates:
        r = requests.get(f"{API}/me", headers={**HEADERS_COMMON, **headers}, params=params, timeout=30)
        print(f"  {label:<24} -> HTTP {r.status_code}")
        if r.status_code == 200:
            save("me.json", r.json())
            save("auth_method.json", {"label": label, "header_names": list(headers), "param_names": list(params)})
            return headers, params
        if first_error is None:
            first_error = r.text[:500]
    print("\nNone worked. Server said:\n", first_error)
    print("Copy this output and send it to Claude.")
    sys.exit(1)


# ---------------------------------------------------------------- 2. CRM dump
def extract_items(data):
    """The response shape is undocumented, so handle list or {items/results/data: [...]}."""
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        for key in ("items", "results", "data", "accounts", "contacts", "records"):
            if isinstance(data.get(key), list):
                return data[key]
        for v in data.values():
            if isinstance(v, list):
                return v
    return []


def fetch_all(endpoint, headers, params, raw_name):
    out, seen = [], set()
    page, page_size = 1, 50
    while page <= 50:  # hard safety cap
        r = requests.get(f"{API}/{endpoint}", headers={**HEADERS_COMMON, **headers},
                         params={**params, "page": page, "page_size": page_size}, timeout=30)
        r.raise_for_status()
        data = r.json()
        if page == 1:
            save(raw_name, data)  # keep the raw shape so we can see pagination fields
        items = extract_items(data)
        def key(i):
            return (i.get("account_id") or i.get("contact_id") or i.get("id")
                    or json.dumps(i, sort_keys=True))
        new = [i for i in items if key(i) not in seen]
        if not new:
            break
        for i in new:
            seen.add(key(i))
        out.extend(new)
        if len(items) < page_size:
            break
        page += 1
        time.sleep(0.2)
    return out


# ---------------------------------------------------------------- 3. website scrape
DETAIL_RE = re.compile(r"^/communities/[^/?#]+/?$")
LABELS = {"address", "care offerings", "administrator", "phone"}


def get_soup(url):
    r = requests.get(url, headers=HEADERS_COMMON, timeout=30)
    r.raise_for_status()
    return BeautifulSoup(r.text, "html.parser"), r.text


def detail_links(soup, page_url):
    links = set()
    for a in soup.find_all("a", href=True):
        path = urlparse(urljoin(page_url, a["href"])).path
        if DETAIL_RE.match(path):
            links.add(BASE + path.rstrip("/"))
    return links


def parse_detail(url):
    soup, html = get_soup(url)
    h1 = soup.find("h1")
    lines = [l.strip() for l in soup.get_text("\n").split("\n") if l.strip()]

    def block_after(label):
        idx = next((i for i, l in enumerate(lines) if l.lower() == label), None)
        if idx is None:
            return []
        block = []
        for l in lines[idx + 1:]:
            if l.lower() in LABELS or l.startswith("←"):
                break
            block.append(l)
        return block

    addr = block_after("address")
    street = city = state = zip_ = None
    for i, l in enumerate(addr):
        m = re.match(r"^(.*?),\s*([A-Z]{2})\s+(\d{5}(?:-\d{4})?)$", l)
        if m:
            city, state, zip_ = m.group(1).strip(), m.group(2), m.group(3)
            street = ", ".join(addr[:i]) or None
            break
    admin = block_after("administrator")
    phone = block_after("phone")
    return {
        "url": url,
        "slug": url.rstrip("/").split("/")[-1],
        "name": h1.get_text(strip=True) if h1 else None,
        "street": street,
        "city": city,
        "state": state,
        "zip": zip_,
        "care_offerings": block_after("care offerings"),
        "administrator": admin[0] if admin else None,
        "phone": phone[0] if phone else None,
        "raw_address_lines": addr,
    }, html


def scrape_website():
    print("\n[3] Scraping website...")
    found = {}  # url -> where we found it

    # directory pages: keep going until a page adds nothing new
    page = 1
    while page <= 20:
        url = f"{BASE}/communities?page={page}"
        soup, _ = get_soup(url)
        links = detail_links(soup, url)
        new = links - set(found)
        print(f"  directory page {page}: {len(links)} links ({len(new)} new)")
        if not new:
            break
        for l in new:
            found[l] = f"directory page {page}"
        page += 1
        time.sleep(0.2)

    # homepage + about page: catches locations not listed in the directory (e.g. Findlay)
    for extra in ("/", "/about"):
        soup, _ = get_soup(BASE + extra)
        for l in detail_links(soup, BASE + extra):
            if l not in found:
                found[l] = f"linked from {extra} only (not in directory)"
                print(f"  EXTRA location found on {extra}: {l}")

    locations = []
    for i, url in enumerate(sorted(found)):
        loc, html = parse_detail(url)
        loc["found_on"] = found[url]
        locations.append(loc)
        if i == 0:
            with open(os.path.join(DATA_DIR, "sample_detail.html"), "w") as f:
                f.write(html)
        if not loc["zip"]:
            print(f"  WARNING could not parse address for {url}: {loc['raw_address_lines']}")
        time.sleep(0.2)
    return locations


# ---------------------------------------------------------------- main
if __name__ == "__main__":
    token = os.environ.get("BH_TOKEN") or getpass("Paste your API token (hidden) and press Enter: ").strip()
    headers, params = detect_auth(token)

    print("\n[2] Downloading CRM...")
    accounts = fetch_all("accounts", headers, params, "raw_accounts_page1.json")
    save("crm_accounts.json", accounts)
    contacts = fetch_all("contacts", headers, params, "raw_contacts_page1.json")
    save("crm_contacts.json", contacts)

    locations = scrape_website()
    save("website_locations.json", locations)

    print("\n================ SUMMARY ================")
    print(f"CRM accounts:      {len(accounts)}")
    print(f"CRM contacts:      {len(contacts)}")
    print(f"Website locations: {len(locations)}")
    if accounts:
        print(f"Account fields:    {sorted(accounts[0].keys())}")
    print("\nNext: send Claude the files in the data/ folder.")
