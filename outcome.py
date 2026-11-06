"""Measures how correct the CRM is against the website: the "did we fix it?" scorecard.

    scorecard(locations, accounts)  -> dict of metrics for one CRM state
    projected(locations, accounts, proposals) -> the CRM state if every given proposal were approved
                                                 (simulated in memory with the real executor; no network)
"""
import copy

from config import BELLHAVEN_PARENT_NAME
from matcher import zip5


def _live(a):
    """An account sales would actually use: not a parent, not Inactive, not a resolved duplicate or CHOW history."""
    return (not a["name"].endswith("(Parent Account)") and a.get("status") != "Inactive"
            and not a.get("duplicate_of_account") and not a.get("chow_current_account"))


def scorecard(locations, accounts, contacts):
    """Uses the same matching rules as the pipeline, so 'correct' means what the matcher means."""
    from matcher import R, refresh_rules, score
    refresh_rules()
    bh = next(a["account_id"] for a in accounts if a["name"] == BELLHAVEN_PARENT_NAME)
    live = [a for a in accounts if _live(a)]
    cbya = {}
    for c in contacts:
        cbya.setdefault(c["account_id"], []).append(c)
    matched = set()
    correct = wrong_parent = outdated = missing = dup_extra = 0
    for loc in locations:
        accs = []
        for a in live:
            r = score(loc, a, cbya.get(a["account_id"], []))
            if r and r[0] >= R["match_threshold"]:
                accs.append(a)
        matched.update(a["account_id"] for a in accs)
        if not accs:
            missing += 1
            continue
        dup_extra += len(accs) - 1
        bad_parent = any(a["parent_id"] != bh for a in accs)
        bad_data = any(a["name"] != loc["name"] or zip5(a["billing_zip"]) != zip5(loc["zip"]) for a in accs)
        wrong_parent += bad_parent
        outdated += bad_data
        correct += (len(accs) == 1 and not bad_parent and not bad_data)
    gone_unflagged = sum(1 for a in live if a["parent_id"] == bh and a["status"] == "Active" and a["account_id"] not in matched)
    return {
        "website_locations": len(locations), "correct": correct, "missing": missing, "wrong_parent": wrong_parent,
        "outdated": outdated, "duplicates": dup_extra, "gone_unflagged": gone_unflagged,
    }


class _MemCRM:
    def __init__(self, accounts):
        self.db = {a["account_id"]: copy.deepcopy(a) for a in accounts}
        self.n = 0

    def accounts(self, zip=None):
        return [copy.deepcopy(a) for a in self.db.values() if not zip or (a["billing_zip"] or "").startswith(zip)]

    def get_account(self, i):
        return copy.deepcopy(self.db[i])

    def patch_account(self, i, fields):
        self.db[i].update(fields)
        if "parent_id" in fields:
            self.db[i]["parent_name"] = self.db[fields["parent_id"]]["name"] if fields["parent_id"] else ""
        return self.db[i]

    def create_account(self, fields):
        self.n += 1
        i = f"SIMULATED{self.n:04d}"
        self.db[i] = {"account_id": i, "lifetime_revenue": 0, "outstanding_ar": 0, "chow_current_account": "",
                      "duplicate_of_account": "", "note": "", **fields,
                      "parent_name": self.db[fields["parent_id"]]["name"]}
        return self.db[i]


def projected(accounts, proposals):
    """CRM state after approving the given proposals, plus the billing accounts' before/after for the SOP check."""
    from executor import apply
    crm = _MemCRM(accounts)
    for p in proposals:
        apply(p, crm)
    return list(crm.db.values())


def billing_preserved(before, after):
    """Billing SOP check: every account with revenue AND AR keeps its parent; if it needed to move, it is linked via CHOW."""
    a_by = {a["account_id"]: a for a in after}
    rows = []
    for b in before:
        if (b.get("lifetime_revenue") or 0) > 0 and (b.get("outstanding_ar") or 0) > 0:
            a = a_by[b["account_id"]]
            rows.append({"account": b["name"], "revenue": b["lifetime_revenue"], "ar": b["outstanding_ar"],
                         "parent_unchanged": a["parent_id"] == b["parent_id"],
                         "chow_link": a.get("chow_current_account") or ""})
    return rows


# ------------------------------------------------------------------ root causes (business insight)
CAUSES = {
    "acq": ("🏢 Acquisition leftovers", "Records still under Harborview / Cedar Trail after Bellhaven bought them (wrong parent, old copies)"),
    "rename": ("✏️ Renamed, CRM never updated", "The facility got a new name; the CRM kept the old one"),
    "new": ("🆕 New community never added or linked", "Opened or joined Bellhaven, but nobody created / linked it in the CRM"),
    "left": ("🚪 Left Bellhaven", "Sold or closed, but still listed under Bellhaven"),
    "entry": ("⌨️ Data-entry errors", "Typos and the same facility entered twice"),
}


def root_cause(p):
    from config import ACQUIRED_FAMILIES
    accs = p.get("accounts") or []
    if p["kind"] in ("CHOW_LINK", "STALE"):
        return "left"
    if p["kind"] == "CREATE" or (p["kind"] == "REPARENT" and accs and not accs[0].get("parent_id")):
        return "new"
    if accs and accs[0].get("parent_name") in ACQUIRED_FAMILIES:
        return "acq"
    if p["kind"] == "UPDATE" and any(ch["field"] == "name" for ch in p["changes"]):
        return "rename"
    return "entry"


def insights(proposals):
    rows = {}
    for p in proposals:
        rows.setdefault(root_cause(p), []).append(p)
    money = [p["accounts"][0] for p in proposals if p["kind"] in ("CHOW", "CHOW_LINK")]
    return {
        "causes": [(CAUSES[k][0], CAUSES[k][1], len(rows.get(k, [])), [p["title"] for p in rows.get(k, [])])
                   for k in CAUSES if rows.get(k)],
        "billing_revenue": sum(a.get("lifetime_revenue") or 0 for a in money),
        "billing_ar": sum(a.get("outstanding_ar") or 0 for a in money),
        "billing_accounts": [a["name"] for a in money],
    }
