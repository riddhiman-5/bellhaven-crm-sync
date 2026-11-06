"""Applies ONE approved proposal to the CRM, with safety checks first.

Before writing anything it re-reads every account involved and checks it still looks the way
it did when the proposal was made (e.g. the parent hasn't already been changed by someone else,
the account still has revenue AND AR for a CHOW). If anything drifted, nothing is written and
the reviewer is told to re-run the pipeline.
"""
from datetime import date

from matcher import norm_street, zip5


class ApplyError(Exception):
    pass


def _norm(v):
    return "" if v is None else str(v)


def preflight(proposal, crm):
    for op in proposal["ops"]:
        if op["op"] == "patch":
            cur = crm.get_account(op["account_id"])
            for field, expected in op.get("expect", {}).items():
                if _norm(cur.get(field)) != _norm(expected):
                    raise ApplyError(f"'{cur['name']}': {field} is now {cur.get(field)!r}, proposal expected {expected!r}. "
                                     "The CRM changed since this proposal was made; re-run the pipeline.")
            if op.get("require_money") and not ((cur.get("lifetime_revenue") or 0) > 0 and (cur.get("outstanding_ar") or 0) > 0):
                raise ApplyError(f"'{cur['name']}' no longer has both revenue and outstanding AR, so the CHOW path "
                                 "no longer applies; re-run the pipeline.")
        elif op["op"] == "create":
            g = op["guard"]
            existing = [a for a in crm.accounts(zip=zip5(g["zip"]))
                        if norm_street(a["billing_street"]) == norm_street(g["street"])
                        and a["account_id"] not in g.get("allow", [])
                        and not a.get("duplicate_of_account") and not a.get("chow_current_account")
                        and not a["name"].endswith("(Parent Account)")
                        and not (a.get("status") == "Inactive" and UNDO_MARKER in (a.get("note") or ""))]
            if existing:
                names = ", ".join(f"'{a['name']}' ({a['account_id']})" for a in existing)
                raise ApplyError(f"An account already exists at {g['street']} {g['zip']}: {names}. Not creating a duplicate; re-run the pipeline.")


UNDO_MARKER = "[bellhaven-sync undo]"


def apply(proposal, crm, reviewer_note=None):
    """Write one approved proposal. Returns (log text, undo steps).

    Undo steps hold the exact values each field had right before we changed it, so the
    change can be reversed later with undo()."""
    preflight(proposal, crm)
    refs, log, undo_steps = {}, [], []
    rn = f" Reviewer note: {reviewer_note.strip()}" if reviewer_note and reviewer_note.strip() else ""
    for op in proposal["ops"]:
        fields = {k: (refs.get(v[1:], v) if isinstance(v, str) and v.startswith("$") else v) for k, v in op["set"].items()}
        if rn and "note" in fields and op.get("touch_note", True):
            fields["note"] = fields["note"] + rn  # reviewer's own note travels into the CRM with the change
        if op["op"] == "create":
            created = crm.create_account(fields)
            new_id = created.get("account_id") if isinstance(created, dict) else None
            if not new_id:
                raise ApplyError(f"Create returned no account_id: {created!r}")
            refs[op["ref"]] = new_id
            undo_steps.append({"op": "retire", "account_id": new_id, "name": fields.get("name")})
            log.append(f"Created account {new_id} '{fields['name']}'")
        else:
            acc_id = op["account_id"]
            before = crm.get_account(acc_id)
            if not op.get("touch_note", True):
                fields.pop("note", None)
            elif "note" in fields:
                old = before.get("note") or ""
                fields["note"] = (old + "\n" if old else "") + fields["note"]
            crm.patch_account(acc_id, fields)
            undo_steps.append({"op": "restore", "account_id": acc_id, "name": before.get("name"),
                               "restore": {k: before.get(k) for k in fields},
                               "written": {k: v for k, v in fields.items() if k != "note"}})
            log.append(f"Updated {acc_id}: " + ", ".join(f"{k}={v!r}" for k, v in fields.items() if k != "note"))
    return f"{date.today().isoformat()}: " + " | ".join(log), undo_steps


def legacy_undo_steps(proposal):
    """For changes applied before undo was added: rebuild from the values the proposal expected to find."""
    steps = []
    for op in proposal["ops"]:
        if op["op"] != "patch" or any(isinstance(v, str) and v.startswith("$") for v in op["set"].values()):
            return None
        written = {k: v for k, v in op["set"].items() if k != "note"}
        steps.append({"op": "restore", "account_id": op["account_id"], "name": op["account_id"],
                      "restore": {k: op.get("expect", {}).get(k, "") for k in written}, "written": written})
    return steps


def undo(undo_steps, crm):
    """Reverse an applied change. Refuses if someone edited the account after we wrote to it."""
    for st in undo_steps:  # check everything first, write nothing if any check fails
        cur = crm.get_account(st["account_id"])
        for k, v in st.get("written", {}).items():
            if _norm(cur.get(k)) != _norm(v):
                raise ApplyError(f"'{cur.get('name')}': {k} is now {cur.get(k)!r}, not the {v!r} we wrote. "
                                 "Someone changed it since; undo stopped so their change isn't overwritten.")
    log = []
    for st in reversed(undo_steps):
        if st["op"] == "retire":
            old = crm.get_account(st["account_id"]).get("note") or ""
            crm.patch_account(st["account_id"], {
                "status": "Inactive",
                "note": (old + "\n" if old else "") + f"{UNDO_MARKER} {date.today().isoformat()} Created in error; "
                                                      "approval undone (API has no delete)."})
            log.append(f"Retired {st['account_id']} '{st['name']}' (set Inactive)")
        else:
            crm.patch_account(st["account_id"], st["restore"])
            log.append(f"Restored {st['account_id']}: " + ", ".join(f"{k}={v!r}" for k, v in st["restore"].items() if k != "note"))
    return f"Undone {date.today().isoformat()}: " + " | ".join(log)
