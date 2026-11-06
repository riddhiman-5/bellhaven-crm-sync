"""Daily pipeline: scrape website -> pull CRM -> match -> queue proposals for review.

Read-only against the CRM. Writes happen only from the review app, after a human approves.

    python3 pipeline.py            # live run
    python3 pipeline.py --offline  # use the last saved snapshots in data/ (no network)
"""
import argparse
import json
import os

from config import DATA_DIR, token
from db import record_run
from matcher import build_proposals


def _save(name, obj):
    os.makedirs(DATA_DIR, exist_ok=True)
    with open(os.path.join(DATA_DIR, name), "w") as f:
        json.dump(obj, f, indent=2)


def _load(name):
    with open(os.path.join(DATA_DIR, name)) as f:
        return json.load(f)


def run(offline=False):
    if offline:
        locations, accounts, contacts = _load("website_locations.json"), _load("crm_accounts.json"), _load("crm_contacts.json")
    else:
        from crm import CRM
        from scraper import scrape
        locations = scrape()
        crm = CRM(token())
        accounts, contacts = crm.accounts(), crm.contacts()
        _save("website_locations.json", locations)
        _save("crm_accounts.json", accounts)
        _save("crm_contacts.json", contacts)

    proposals, confirmed = build_proposals(locations, accounts, contacts)
    summary = record_run(proposals, confirmed, {
        "website_locations": len(locations), "crm_accounts": len(accounts), "crm_contacts": len(contacts),
        "mode": "offline" if offline else "live"})
    print(json.dumps(summary, indent=2))
    return summary


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--offline", action="store_true", help="use saved snapshots in data/ instead of the network")
    run(ap.parse_args().offline)
