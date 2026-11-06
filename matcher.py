"""Matching + classification. Pure logic: takes website locations and CRM data, returns proposals.

Nothing here talks to the network, so it can be tested offline and re-run safely.

Core idea: buildings don't move, names do. A website location and a CRM account are the
same facility when the STREET ADDRESS matches (after normalising "Ave"/"Avenue", "NW"/"Northwest"...).
Phone, administrator name and facility name are supporting evidence, and they rescue cases
where the CRM address is odd (e.g. a billing PO Box).
"""
import difflib
import hashlib
import json
import os
import re
from datetime import date

from config import ACQUIRED_FAMILIES, BELLHAVEN_PARENT_NAME

# Three zones (the "reconsideration" design):
#   score >= 75            confident match: address + at least one more independent clue
#   50 <= score < 75       match, but RECONSIDER: only the address agrees -> reviewer should look harder / verify
#   20 <= closest < 50     (new accounts only) POSSIBLE match: a look-alike exists -> check before creating
# All weights and thresholds live in rules.json so the team can tune them without touching code.

DEFAULT_RULES = {
    "match_threshold": 50, "confident_threshold": 75, "possible_threshold": 20,
    "weights": {"street_zip": 60, "street_city": 50, "zip_only": 10, "city": 5,
                "phone": 20, "admin_contact": 25, "name_max": 15},
}
RULES_PATH = os.environ.get("BH_RULES", os.path.join(os.path.dirname(os.path.abspath(__file__)), "rules.json"))


def load_rules(path=None):
    rules = json.loads(json.dumps(DEFAULT_RULES))
    p = path or RULES_PATH
    if os.path.exists(p):
        with open(p) as f:
            user = json.load(f)
        rules.update({k: v for k, v in user.items() if k != "weights" and not k.startswith("_")})
        rules["weights"].update(user.get("weights", {}))
    return rules


R = load_rules()


def refresh_rules(rules=None):
    """Reload rules.json (or use the given rules dict, e.g. for a preview)."""
    R.clear()
    R.update(rules or load_rules())

ABBR = {
    "street": "st", "avenue": "ave", "av": "ave", "road": "rd", "drive": "dr", "lane": "ln",
    "boulevard": "blvd", "court": "ct", "place": "pl", "pike": "pike", "pk": "pike",
    "parkway": "pkwy", "highway": "hwy", "circle": "cir", "terrace": "ter",
    "north": "n", "south": "s", "east": "e", "west": "w",
    "northwest": "nw", "northeast": "ne", "southwest": "sw", "southeast": "se",
}
NAME_STOPWORDS = {"bellhaven", "of", "the", "at", "and", "a"}

# website care offering -> CRM care_type vocabulary
CARE_MAP = {
    "Short-Term Rehabilitation & Nursing": "Skilled Nursing",
    "Memory Support": "Memory Care",
    "Assisted Living": "Assisted Living",
}

# Display order + plain-English rule for each proposal type (shown to the reviewer)
KINDS = {
    "CHOW": ("💰 Change of ownership (CHOW)", "Facility belongs under Bellhaven, but the account has revenue history AND outstanding AR. Billing SOP: leave the old account untouched, create a new account under Bellhaven, and point the old account's chow_current_account at it."),
    "CHOW_LINK": ("💰 Sold - link to new owner's account", "Account left Bellhaven's website and another operator already has an account at the same address. It has revenue AND outstanding AR, so per the billing SOP the old account is left untouched and chow_current_account points to the new owner's existing account (no duplicate is created)."),
    "REPARENT": ("🔀 Move under Bellhaven", "Facility is on Bellhaven's website but the CRM links it to another parent. No outstanding AR (or no revenue history), so the SOP allows re-parenting the existing account directly."),
    "UPDATE": ("✏️ Fix outdated data", "Same facility (address matches), already under Bellhaven, but the CRM name or zip differs from the operator's own website."),
    "DUPLICATE": ("👯 Duplicate account", "Several CRM accounts describe the same building. One survives; this copy is marked duplicate_of_account = survivor and set Inactive (the API has no merge/delete)."),
    "CREATE": ("🆕 New account", "Location is on Bellhaven's website but no CRM account matches its address."),
    "STALE": ("⚠️ No longer on website", "Account sits under Bellhaven in the CRM but the location is no longer listed on Bellhaven's website, and no other operator is found at that address. We don't know if it closed or was sold, so a human should review rather than the system guessing."),
}
KIND_ORDER = list(KINDS)


# ------------------------------------------------------------------ normalisation
def norm_street(s):
    s = re.sub(r"[^\w\s]", " ", (s or "").lower())
    return " ".join(ABBR.get(t, t) for t in s.split())


def verbatim(s):
    return re.sub(r"[^a-z0-9]", "", (s or "").lower())


def zip5(z):
    return (z or "").strip()[:5]


def digits(p):
    return re.sub(r"\D", "", p or "")[-10:]


def name_key(n):
    n = (n or "").lower().replace("&", " and ").replace("centre", "center").replace("rehab ", "rehabilitation ")
    toks = [t for t in re.sub(r"[^\w\s]", " ", n).split() if t not in NAME_STOPWORDS]
    return " ".join(toks)


def name_similarity(a, b):
    return difflib.SequenceMatcher(None, name_key(a), name_key(b)).ratio()


def money(a):
    return (a.get("lifetime_revenue") or 0) > 0 and (a.get("outstanding_ar") or 0) > 0


def fingerprint(kind, key, sets):
    """Stable id for a proposal: type + account/location + the target values.
    Notes are excluded (they contain dates and names), so the same decision is recognised on every run, any day."""
    clean = {k: v for k, v in sets.items() if k != "note" and not k.endswith(".note")}
    raw = json.dumps([kind, key, clean], sort_keys=True)
    return hashlib.sha1(raw.encode()).hexdigest()[:16]


def proposal_key(p):
    """The account (or website location, for new accounts) a proposal is about."""
    if p.get("key"):
        return p["key"]
    return p["location"]["slug"] if p["kind"] == "CREATE" else p["accounts"][0]["account_id"]


def fingerprint_of(p):
    sets = {}
    for op in p["ops"]:
        sets.update({f"{op.get('account_id', op.get('ref'))}.{k}": v for k, v in op["set"].items()})
    return fingerprint(p["kind"], proposal_key(p), sets)


# ------------------------------------------------------------------ scoring
def score(loc, acc, contacts):
    """Return (score, evidence list, flags) or None if the account can't be this location."""
    if (acc.get("billing_state") or "").upper() != (loc["state"] or "").upper():
        return None
    ev, s, parts = [], 0, []
    same_street = norm_street(acc["billing_street"]) == norm_street(loc["street"])
    same_zip = zip5(acc["billing_zip"]) == zip5(loc["zip"])
    same_city = (acc["billing_city"] or "").lower() == (loc["city"] or "").lower()
    if same_street and same_zip:
        w = R["weights"]["street_zip"]
        s += w
        parts.append(("Street + zip match", w))
        ev.append(f"✅ Street + zip match: CRM '{acc['billing_street']}, {acc['billing_zip']}' = website '{loc['street']}, {loc['zip']}'")
    elif same_street and same_city:
        w = R["weights"]["street_city"]
        s += w
        parts.append(("Street + city match (zip differs)", w))
        ev.append(f"⚠️ Street + city match but zip differs: CRM {acc['billing_zip']} vs website {loc['zip']}")
    elif same_zip:
        w = R["weights"]["zip_only"]
        s += w
        parts.append(("Same zip only", w))
        ev.append(f"➖ Same zip, different street: CRM '{acc['billing_street']}' vs website '{loc['street']}'")
    else:
        return None  # different street AND different zip: not the same building
    if same_city:
        s += R["weights"]["city"]
        parts.append(("Same city", R["weights"]["city"]))
    phone_match = bool(digits(acc.get("phone"))) and digits(acc.get("phone")) == digits(loc.get("phone"))
    if phone_match:
        s += R["weights"]["phone"]
        parts.append(("Phone matches", R["weights"]["phone"]))
        ev.append(f"✅ Phone matches website: {loc['phone']}")
    admin = (loc.get("administrator") or "").strip().lower()
    admin_contact = next((c for c in contacts if c["name"].strip().lower() == admin), None) if admin else None
    if admin_contact:
        s += R["weights"]["admin_contact"]
        parts.append(("Administrator is a CRM contact", R["weights"]["admin_contact"]))
        ev.append(f"✅ Website administrator '{loc['administrator']}' is a CRM contact on this account ({admin_contact['title']})")
    sim = name_similarity(acc["name"], loc["name"])
    name_pts = round(R["weights"]["name_max"] * sim)
    s += name_pts
    parts.append((f"Name similarity {sim:.0%}", name_pts))
    ev.append(f"Name similarity {sim:.0%}: CRM '{acc['name']}' vs website '{loc['name']}'")
    flags = {
        "phone": phone_match, "admin": bool(admin_contact),
        "verbatim_street": verbatim(acc["billing_street"]) == verbatim(loc["street"]),
        "parts": parts, "score": s,
    }
    return s, ev, flags


def survivor_rank(acc, flags, n_contacts, bh_id):
    """Which duplicate survives: billing history > contacts > already correct parent >
    phone/admin evidence > cleanest address > acquisition lineage."""
    lineage = ACQUIRED_FAMILIES[::-1].index(acc["parent_name"]) + 1 if acc["parent_name"] in ACQUIRED_FAMILIES else 0
    has_money = (acc.get("lifetime_revenue") or 0) > 0 or (acc.get("outstanding_ar") or 0) > 0
    return (has_money, n_contacts, acc["parent_id"] == bh_id,
            int(flags["phone"]) + int(flags["admin"]), flags["verbatim_street"], lineage)


def snapshot(a):
    keep = ["account_id", "name", "parent_id", "parent_name", "billing_street", "billing_city", "billing_state",
            "billing_zip", "care_type", "status", "phone", "lifetime_revenue", "outstanding_ar",
            "chow_current_account", "duplicate_of_account", "note"]
    return {k: a.get(k) for k in keep}


# ------------------------------------------------------------------ main entry
def build_proposals(locations, accounts, contacts, today=None, rules=None):
    refresh_rules(rules)
    today = today or date.today().isoformat()
    tag = f"[bellhaven-sync {today}]"
    by_id = {a["account_id"]: a for a in accounts}
    cbya = {}
    for c in contacts:
        cbya.setdefault(c["account_id"], []).append(c)
    bh = next(a for a in accounts if a["name"] == BELLHAVEN_PARENT_NAME)
    bh_id = bh["account_id"]

    def is_parent(a):
        return a["name"].endswith("(Parent Account)")

    # Accounts already resolved as duplicates or CHOW'd are historical records: never re-match them.
    # Accounts we created and then retired via Undo are ignored too.
    eligible = [a for a in accounts if not is_parent(a)
                and not a.get("duplicate_of_account") and not a.get("chow_current_account")
                and not (a.get("status") == "Inactive" and "[bellhaven-sync undo]" in (a.get("note") or ""))]

    proposals, confirmed, matched = [], [], set()

    def best_contact(acc_id):
        """Who to email for verification: Administrator / Executive Director first, with an email if possible."""
        rank = {"Administrator": 0, "Executive Director": 1}
        cs = sorted(cbya.get(acc_id, []), key=lambda c: (not c.get("email"), rank.get(c.get("title"), 9)))
        return {k: cs[0].get(k) for k in ("name", "title", "email", "phone")} if cs else None

    def add(kind, key, title, ops, evidence, loc=None, accs=(), changes=(), confidence="high", extra=None):
        sets = {}
        for op in ops:
            sets.update({f"{op.get('account_id', op.get('ref'))}.{k}": v for k, v in op["set"].items()})
        proposals.append({
            "fingerprint": fingerprint(kind, key, sets), "key": key, "kind": kind, "label": KINDS[kind][0],
            "rule": KINDS[kind][1], "title": title, "confidence": confidence,
            "location": loc, "accounts": [snapshot(a) for a in accs],
            "contact": best_contact(accs[0]["account_id"]) if accs else None,
            "changes": list(changes), "ops": ops, "evidence": evidence, **(extra or {}),
        })

    for loc in locations:
        scored = []
        closest = None
        for a in eligible:
            r = score(loc, a, cbya.get(a["account_id"], []))
            if not r:
                continue
            if r[0] >= R["match_threshold"]:
                scored.append((r, a))
            elif not closest or r[0] > closest[0][0]:
                closest = (r, a)

        # ---------- no CRM account: create one
        if not scored:
            fields = {
                "name": loc["name"], "parent_id": bh_id,
                "billing_street": loc["street"], "billing_city": loc["city"],
                "billing_state": loc["state"], "billing_zip": loc["zip"],
                "care_type": CARE_MAP.get((loc["care_offerings"] or [""])[0], ""),
                "phone": loc.get("phone") or "", "status": "Active",
                "note": f"{tag} Created from Bellhaven website listing {loc['url']}",
            }
            ev = [f"No CRM account at {loc['street']}, {loc['city']}, {loc['state']} {loc['zip']}",
                  f"Found on website: {loc['found_on']}"]
            if closest:
                (s, cev, _), ca = closest
                ev.append(f"Closest CRM candidate rejected (score {s} < {R['match_threshold']}): '{ca['name']}' at {ca['billing_street']}, {ca['billing_city']} under {ca['parent_name'] or 'no parent'}")
                ev += ["   " + e for e in cev]
            same_name = [a for a in accounts if name_key(a["name"]) == name_key(loc["name"])]
            for a in same_name:
                ev.append(f"Same-name account '{a['name']}' exists in {a['billing_city']}, {a['billing_state']}: different facility, not matched")
            create_extra = {"score_parts": closest[0][2]["parts"], "score": closest[0][0],
                            "score_of": f"closest CRM account '{closest[1]['name']}'"} if closest else {}
            create_conf = "high"
            if closest and closest[0][0] >= R["possible_threshold"]:
                create_conf = "reconsider"
                ev.insert(0, f"🔁 Reconsider: closest CRM account '{closest[1]['name']}' scored {closest[0][0]} "
                             f"(between {R['possible_threshold']} and {R['match_threshold']}). Check it really is a different facility before creating.")
            add("CREATE", loc["slug"], f"Create '{loc['name']}' ({loc['city']}, {loc['state']}) under Bellhaven",
                [{"op": "create", "ref": "new", "set": fields, "guard": {"street": loc["street"], "zip": loc["zip"]}}],
                ev, loc=loc, changes=[{"account": "(new account)", "field": k, "before": "", "after": v}
                                     for k, v in fields.items() if k != "note"], extra=create_extra, confidence=create_conf)
            continue

        # ---------- one or more matches: choose survivor
        scored.sort(key=lambda x: survivor_rank(x[1], x[0][2], len(cbya.get(x[1]["account_id"], [])), bh_id), reverse=True)
        (s, ev, flags), acc = scored[0]
        for _, a in scored:
            matched.add(a["account_id"])
        sx = {"score_parts": flags["parts"], "score": s, "score_of": f"CRM account '{acc['name']}'"}
        confidence = "high" if s >= R["confident_threshold"] else "reconsider"
        base_ev = [f"Matched website location '{loc['name']}' ({loc['url']}) - match score {s}"] + ev
        if len(scored) > 1:
            base_ev.append(f"{len(scored)} CRM accounts share this address; '{acc['name']}' chosen as survivor "
                           "(rule: billing history > contacts > already under Bellhaven > phone/admin evidence > cleanest address > acquisition lineage)")

        fix = {}
        if acc["name"] != loc["name"]:
            fix["name"] = loc["name"]
        if zip5(acc["billing_zip"]) != zip5(loc["zip"]):
            fix["billing_zip"] = loc["zip"]
        if acc["status"] != "Active":
            fix["status"] = "Active"

        if acc["parent_id"] != bh_id and money(acc):
            new = {
                "name": loc["name"], "parent_id": bh_id,
                "billing_street": loc["street"], "billing_city": loc["city"],
                "billing_state": loc["state"], "billing_zip": loc["zip"],
                "care_type": acc.get("care_type") or CARE_MAP.get((loc["care_offerings"] or [""])[0], ""),
                "phone": loc.get("phone") or acc.get("phone") or "", "status": "Active",
                "note": f"{tag} New account after change of ownership. Previous account {acc['account_id']} "
                        f"({acc['parent_name']}) kept for billing: revenue ${acc['lifetime_revenue']:,}, AR ${acc['outstanding_ar']:,}.",
            }
            ops = [
                {"op": "create", "ref": "new", "set": new, "guard": {"street": loc["street"], "zip": loc["zip"], "allow": [acc["account_id"]]}},
                {"op": "patch", "account_id": acc["account_id"], "set": {"chow_current_account": "$new"},
                 "expect": {"chow_current_account": "", "parent_id": acc["parent_id"]}, "require_money": True, "touch_note": False},
            ]
            add("CHOW", acc["account_id"], f"CHOW: '{acc['name']}' moves from {acc['parent_name']} to Bellhaven",
                ops, base_ev + [f"💵 Revenue ${acc['lifetime_revenue']:,} AND outstanding AR ${acc['outstanding_ar']:,} > 0 -> old account must be preserved"],
                loc=loc, accs=[acc], confidence=confidence, extra=sx,
                changes=[{"account": "(new account)", "field": k, "before": "", "after": v} for k, v in new.items() if k != "note"]
                + [{"account": acc["name"], "field": "chow_current_account", "before": "", "after": "(id of new account)"}])
        else:
            if acc["parent_id"] != bh_id:
                fix["parent_id"] = bh_id
                why = ("no revenue history" if not acc.get("lifetime_revenue") else "outstanding AR is $0")
                base_ev.append(f"💵 Revenue ${acc.get('lifetime_revenue') or 0:,}, AR ${acc.get('outstanding_ar') or 0:,} -> {why}, re-parent directly")
            if fix:
                kind = "REPARENT" if "parent_id" in fix else "UPDATE"
                title = (f"Move '{acc['name']}' from {acc['parent_name'] or 'no parent'} to Bellhaven" if kind == "REPARENT"
                         else f"Update '{acc['name']}' ({loc['city']}, {loc['state']})")
                note = f"{tag} Synced to Bellhaven website ({loc['url']}): " + ", ".join(f"{k} {acc.get(k) or '(blank)'!r} -> {v!r}" for k, v in fix.items())
                add(kind, acc["account_id"], title,
                    [{"op": "patch", "account_id": acc["account_id"], "set": {**fix, "note": note},
                      "expect": {k: acc.get(k) or "" for k in fix}}],
                    base_ev, loc=loc, accs=[acc], confidence=confidence, extra=sx,
                    changes=[{"account": acc["name"], "field": k,
                              "before": (acc.get("parent_name") or "(none)") if k == "parent_id" else acc.get(k),
                              "after": BELLHAVEN_PARENT_NAME if k == "parent_id" else v} for k, v in fix.items()])
            else:
                confirmed.append({"location": loc["name"], "city": f"{loc['city']}, {loc['state']}",
                                  "account_id": acc["account_id"], "crm_name": acc["name"], "score": s,
                                  "evidence": " | ".join(ev[:3])})

        for (ls, lev, lflags), loser in scored[1:]:
            add("DUPLICATE", loser["account_id"],
                f"Mark '{loser['name']}' [{loser['account_id'][-6:]}] ({loser['parent_name'] or 'no parent'}) "
                f"as duplicate of '{acc['name']}' [{acc['account_id'][-6:]}]",
                [{"op": "patch", "account_id": loser["account_id"],
                  "set": {"duplicate_of_account": acc["account_id"], "status": "Inactive",
                          "note": f"{tag} Duplicate of {acc['account_id']} ('{acc['name']}'): same building as Bellhaven website listing {loc['url']}"},
                  "expect": {"duplicate_of_account": "", "status": loser["status"]}}],
                [f"Same building as '{loc['name']}' ({loc['street']}, {loc['city']} {loc['zip']}) - match score {ls}"] + lev
                + [f"Survivor: '{acc['name']}' ({acc['account_id']}, {acc['parent_name'] or 'no parent'}, "
                   f"{len(cbya.get(acc['account_id'], []))} contacts, revenue ${acc.get('lifetime_revenue') or 0:,})",
                   f"This copy: {len(cbya.get(loser['account_id'], []))} contacts, revenue ${loser.get('lifetime_revenue') or 0:,}, AR ${loser.get('outstanding_ar') or 0:,}"],
                loc=loc, accs=[loser, acc], confidence="high" if ls >= R["confident_threshold"] else "reconsider",
                extra={"score_parts": lflags["parts"], "score": ls, "score_of": f"duplicate '{loser['name']}'"},
                changes=[{"account": loser["name"], "field": "duplicate_of_account", "before": "", "after": f"{acc['account_id']} ({acc['name']})"},
                         {"account": loser["name"], "field": "status", "before": loser["status"], "after": "Inactive"}])

    # ---------- accounts under Bellhaven that are no longer on the website
    for a in eligible:
        if a["parent_id"] != bh_id or a["account_id"] in matched or a["status"] == "Inactive":
            continue
        other = [b for b in eligible if b["account_id"] != a["account_id"] and b["parent_id"] != bh_id
                 and norm_street(b["billing_street"]) == norm_street(a["billing_street"])
                 and zip5(b["billing_zip"]) == zip5(a["billing_zip"])]
        ev = [f"'{a['name']}' ({a['billing_street']}, {a['billing_city']}, {a['billing_state']}) is under Bellhaven in the CRM "
              f"but not among the {len(locations)} locations on Bellhaven's website"]
        if other:
            o = other[0]
            ev.append(f"🏢 Another operator's account is at the same address: '{o['name']}' under {o['parent_name'] or 'no parent'} ({o['account_id']}) -> facility was sold")
            if money(a):
                ev.append(f"💵 Revenue ${a['lifetime_revenue']:,} AND AR ${a['outstanding_ar']:,} > 0 -> keep old account untouched, link via chow_current_account")
                add("CHOW_LINK", a["account_id"], f"Sold: link '{a['name']}' to new owner's account '{o['name']}'",
                    [{"op": "patch", "account_id": a["account_id"], "set": {"chow_current_account": o["account_id"]},
                      "expect": {"chow_current_account": "", "parent_id": a["parent_id"]}, "require_money": True, "touch_note": False}],
                    ev, accs=[a, o],
                    changes=[{"account": a["name"], "field": "chow_current_account", "before": "", "after": f"{o['account_id']} ({o['name']})"}])
            else:
                ev.append("💵 No outstanding AR -> no need to preserve; mark as duplicate of the new owner's account")
                add("DUPLICATE", a["account_id"], f"Sold: mark '{a['name']}' as duplicate of '{o['name']}'",
                    [{"op": "patch", "account_id": a["account_id"],
                      "set": {"duplicate_of_account": o["account_id"], "status": "Inactive",
                              "note": f"{tag} Left Bellhaven; now operated as {o['account_id']} ('{o['name']}')"},
                      "expect": {"duplicate_of_account": "", "status": a["status"]}}],
                    ev, accs=[a, o],
                    changes=[{"account": a["name"], "field": "duplicate_of_account", "before": "", "after": o["account_id"]},
                             {"account": a["name"], "field": "status", "before": a["status"], "after": "Inactive"}])
        elif a["status"] != "Needs Review":
            ev.append("No other operator found at this address -> owner unknown (closed or sold?)")
            add("STALE", a["account_id"], f"Flag '{a['name']}' ({a['billing_city']}, {a['billing_state']}) - no longer on Bellhaven website",
                [{"op": "patch", "account_id": a["account_id"],
                  "set": {"status": "Needs Review",
                          "note": f"{tag} Not listed on Bellhaven website; new owner unknown. Verify whether closed or sold before re-parenting."},
                  "expect": {"status": a["status"]}}],
                ev, accs=[a], confidence="reconsider",
                changes=[{"account": a["name"], "field": "status", "before": a["status"], "after": "Needs Review"}])

    proposals.sort(key=lambda p: (KIND_ORDER.index(p["kind"]), p["title"]))
    return proposals, confirmed
