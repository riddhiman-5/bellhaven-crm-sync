"""Thin client for the CRM sandbox API."""
import time

import requests

from config import API, USER_AGENT


def _unwrap(data):
    if isinstance(data, dict) and isinstance(data.get("data"), dict):
        return data["data"]
    return data


class CRM:
    def __init__(self, token):
        self.s = requests.Session()
        self.s.headers.update({"Authorization": f"Bearer {token}", "User-Agent": USER_AGENT})

    def _list(self, endpoint, id_field, **params):
        out, seen, page = [], set(), 1
        while page <= 100:
            r = self.s.get(f"{API}/{endpoint}", params={**params, "page": page, "page_size": 50}, timeout=30)
            r.raise_for_status()
            body = r.json()
            items = body.get("data", []) if isinstance(body, dict) else body
            new = [i for i in items if i[id_field] not in seen]
            if not new:
                break
            for i in new:
                seen.add(i[id_field])
            out.extend(new)
            total = body.get("total") if isinstance(body, dict) else None
            if (total is not None and len(out) >= total) or len(items) < 50:
                break
            page += 1
            time.sleep(0.1)
        return out

    def accounts(self, **filters):
        return self._list("accounts", "account_id", **filters)

    def contacts(self):
        return self._list("contacts", "contact_id")

    def get_account(self, account_id):
        r = self.s.get(f"{API}/accounts/{account_id}", timeout=30)
        r.raise_for_status()
        return _unwrap(r.json())

    def patch_account(self, account_id, fields):
        r = self.s.patch(f"{API}/accounts/{account_id}", json=fields, timeout=30)
        if r.status_code >= 400:
            raise RuntimeError(f"PATCH {account_id} failed ({r.status_code}): {r.text[:300]}")
        return _unwrap(r.json())

    def create_account(self, fields):
        r = self.s.post(f"{API}/accounts", json=fields, timeout=30)
        if r.status_code >= 400:
            raise RuntimeError(f"POST account failed ({r.status_code}): {r.text[:300]}")
        return _unwrap(r.json())
