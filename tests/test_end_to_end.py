"""End-to-end test against an in-memory fake CRM built from the saved snapshots.

Checks: (1) the expected proposals are produced, (2) approving everything leaves a CRM that
produces ZERO new proposals on the next run, (3) rejected items are never re-proposed,
(4) the CHOW rule leaves old accounts untouched except chow_current_account.

    python3 -m pytest tests/   or   python3 tests/test_end_to_end.py
"""
import copy
import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ["BH_DB"] = os.path.join(tempfile.mkdtemp(), "test_state.db")

import db  # noqa: E402
from executor import ApplyError, apply  # noqa: E402
from matcher import build_proposals  # noqa: E402

DATA = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data")


def load(name):
    with open(os.path.join(DATA, name)) as f:
        return json.load(f)


class FakeCRM:
    def __init__(self, accounts):
        self.db = {a["account_id"]: copy.deepcopy(a) for a in accounts}
        self.n = 0

    def accounts(self, zip=None):
        return [copy.deepcopy(a) for a in self.db.values() if not zip or a["billing_zip"].startswith(zip)]

    def get_account(self, i):
        return copy.deepcopy(self.db[i])

    def patch_account(self, i, fields):
        self.db[i].update(fields)
        if "parent_id" in fields:
            self.db[i]["parent_name"] = self.db[fields["parent_id"]]["name"] if fields["parent_id"] else ""
        return self.db[i]

    def create_account(self, fields):
        self.n += 1
        i = f"001TESTNEW{self.n:08d}"
        acc = {"account_id": i, "lifetime_revenue": 0, "outstanding_ar": 0, "chow_current_account": "",
               "duplicate_of_account": "", "note": "", **fields}
        acc["parent_name"] = self.db[fields["parent_id"]]["name"]
        self.db[i] = acc
        return acc


def main():
    locs, accs, cons = load("website_locations.json"), load("crm_accounts.json"), load("crm_contacts.json")
    crm = FakeCRM(accs)
    before = copy.deepcopy(crm.db)

    # run 1
    props, conf = build_proposals(locs, list(crm.db.values()), cons)
    kinds = sorted(p["kind"] for p in props)
    print("run 1:", len(props), "proposals,", len(conf), "confirmed")
    s1 = db.record_run(props, conf, {"mode": "test"})
    assert s1["new"] == len(props) == 28, s1

    # re-run with no decisions: nothing new, all still pending
    s1b = db.record_run(*build_proposals(locs, list(crm.db.values()), cons), {"mode": "test"})
    assert s1b["new"] == 0 and s1b["still_pending"] == 28, s1b

    # reject one, approve the rest
    pending = db.list_proposals("pending")
    undo_log = []
    rejected = next(r for r in pending if r["kind"] == "STALE")
    db.decide(rejected["fingerprint"], "rejected", reason="test")
    for r in pending:
        if r["fingerprint"] == rejected["fingerprint"]:
            continue
        res, steps = apply(r["payload"], crm)
        db.decide(r["fingerprint"], "applied", result=res, undo=steps)
        undo_log.append(steps)

    # CHOW checks: old accounts untouched except chow_current_account
    for name in ("Bellhaven of Marietta", "Bellhaven of Tiffin", "Bellhaven of Sandusky"):
        old = next(a for a in before.values() if a["name"] == name and a["chow_current_account"] == "")
        now = crm.db[old["account_id"]]
        diff = {k for k in now if now[k] != old[k]}
        assert diff == {"chow_current_account"}, (name, diff)
        assert now["chow_current_account"] in crm.db
        print(f"CHOW ok: {name} -> {crm.db[now['chow_current_account']]['name']} under {crm.db[now['chow_current_account']]['parent_name']}")

    # run 2: corrected CRM should produce no new proposals; the rejected one stays rejected
    props2, conf2 = build_proposals(locs, list(crm.db.values()), cons)
    s2 = db.record_run(props2, conf2, {"mode": "test"})
    print("run 2:", s2)
    assert s2["new"] == 0, [p["title"] for p in props2]
    assert len(props2) == 1 and props2[0]["fingerprint"] == rejected["fingerprint"]
    assert s2["already_decided_skipped"] == 1
    assert len(conf2) == 35 - 0, len(conf2)  # every website location now matches cleanly

    # stale-data guard: approving an already-applied change must refuse
    try:
        apply(next(r for r in db.list_proposals("applied") if r["kind"] == "REPARENT")["payload"], crm)
        raise AssertionError("expected ApplyError")
    except ApplyError as e:
        print("guard ok:", str(e)[:90], "...")
    # undo everything: every original account must be byte-for-byte back to how it was
    from executor import undo
    for steps in reversed(undo_log):
        undo(steps, crm)
    for i, a in before.items():
        assert crm.db[i] == a, (a["name"], {k: (a[k], crm.db[i][k]) for k in a if a[k] != crm.db[i][k]})
    created = [a for i, a in crm.db.items() if i not in before]
    assert created and all(a["status"] == "Inactive" for a in created)
    props3, _ = build_proposals(locs, list(crm.db.values()), cons)
    assert len(props3) == 28, len(props3)  # back to the original state: same 28 fixes proposed again
    print("undo ok: all", len(before), "original accounts restored exactly;", len(created), "created accounts retired")
    print("ALL TESTS PASSED")


def test_end_to_end():
    main()




def test_dues_cleared_turns_chow_into_reparent():
    """If Marietta pays its past dues, the next run withdraws the CHOW and proposes a plain re-parent."""
    os.environ["BH_DB"] = os.path.join(tempfile.mkdtemp(), "dues.db")
    import importlib

    import config
    importlib.reload(config)
    importlib.reload(db)
    locs, accs, cons = load("website_locations.json"), load("crm_accounts.json"), load("crm_contacts.json")
    db.record_run(*build_proposals(locs, accs, cons), {"mode": "test"})
    for a in accs:
        if a["name"] == "Bellhaven of Marietta":
            a["outstanding_ar"] = 0
    props, conf = build_proposals(locs, accs, cons)
    s = db.record_run(props, conf, {"mode": "test"})
    marietta = [p for p in props if "Marietta" in p["title"]]
    assert [p["kind"] for p in marietta] == ["REPARENT"], marietta
    assert s["superseded"] == 1 and s["new"] == 1, s
    print("dues cleared -> CHOW superseded, REPARENT proposed: ok")


def test_verification_drafts():
    from verification import draft
    props, _ = build_proposals(load("website_locations.json"), load("crm_accounts.json"), load("crm_contacts.json"))
    for p in props:
        to, subj, body = draft(p)
        assert subj and "Hello" in body
        if p["kind"] in ("CHOW", "CHOW_LINK"):
            assert "outstanding balance" in body, p["title"]
    print("verification drafts ok for all", len(props), "fixes")


def test_scorecard_before_after():
    from outcome import billing_preserved, projected, scorecard
    locs, accs, cons = load("website_locations.json"), load("crm_accounts.json"), load("crm_contacts.json")
    props, _ = build_proposals(locs, accs, cons)
    before, after_accs = scorecard(locs, accs, cons), projected(accs, props)
    after = scorecard(locs, after_accs, cons)
    assert before["correct"] == 13 and after["correct"] == 35, (before, after)
    assert after["duplicates"] == after["wrong_parent"] == after["missing"] == after["gone_unflagged"] == 0
    assert all(r["parent_unchanged"] for r in billing_preserved(accs, after_accs))
    assert len(build_proposals(locs, after_accs, cons)[0]) == 0
    print("scorecard ok: 13/35 -> 35/35, 0 duplicates, billing parents untouched, re-run proposes 0")


def test_rules_are_tunable():
    from matcher import load_rules
    locs, accs, cons = load("website_locations.json"), load("crm_accounts.json"), load("crm_contacts.json")
    base = load_rules()
    assert len(build_proposals(locs, accs, cons, rules=base)[0]) == 28
    strict = json.loads(json.dumps(base)); strict["confident_threshold"] = 100
    n_recon = sum(p["confidence"] != "high" for p in build_proposals(locs, accs, cons, rules=strict)[0])
    assert n_recon > 10, n_recon
    build_proposals(locs, accs, cons)  # back to rules.json
    print("rules tunable ok: confident threshold 100 ->", n_recon, "reconsider")


def test_reviewer_note_written():
    locs, accs, cons = load("website_locations.json"), load("crm_accounts.json"), load("crm_contacts.json")
    crm = FakeCRM(accs)
    p = next(p for p in build_proposals(locs, accs, cons)[0] if p["kind"] == "UPDATE")
    apply(p, crm, reviewer_note="Checked with the regional office.")
    assert "Reviewer note: Checked with the regional office." in crm.db[p["accounts"][0]["account_id"]]["note"]
    print("reviewer note written to CRM ok")


def test_fingerprints_stable_across_days_and_renames():
    locs, accs, cons = load("website_locations.json"), load("crm_accounts.json"), load("crm_contacts.json")
    day1 = {p["fingerprint"] for p in build_proposals(locs, accs, cons, today="2026-11-05")[0]}
    day2 = {p["fingerprint"] for p in build_proposals(locs, accs, cons, today="2026-11-06")[0]}
    assert day1 == day2, "fingerprints must not depend on the date"
    # approving the Kettering survivor's rename must not change the remaining Kettering duplicate proposals
    crm = FakeCRM(accs)
    props = build_proposals(locs, accs, cons)[0]
    surv = next(p for p in props if p["title"].startswith("Move 'Kettering Care Centre'"))
    dups_before = {p["fingerprint"] for p in props if p["kind"] == "DUPLICATE" and "Kettering" in p["title"]}
    apply(surv, crm)
    dups_after = {p["fingerprint"] for p in build_proposals(locs, list(crm.db.values()), cons)[0]
                  if p["kind"] == "DUPLICATE" and "Kettering" in p["title"]}
    assert dups_before == dups_after, "duplicate proposals must survive the survivor being renamed"
    print("fingerprints stable across days and renames: ok")


def test_migrates_old_fingerprints():
    """A database written by the buggy version (fingerprints included note text) is re-keyed, nothing is lost."""
    import hashlib
    os.environ["BH_DB"] = os.path.join(tempfile.mkdtemp(), "old.db")
    import importlib

    import config
    importlib.reload(config); importlib.reload(db)
    locs, accs, cons = load("website_locations.json"), load("crm_accounts.json"), load("crm_contacts.json")
    props = build_proposals(locs, accs, cons, today="2026-11-05")[0]

    def old_fp(p):
        sets = {}
        for op in p["ops"]:
            sets.update({f"{op.get('account_id', op.get('ref'))}.{k}": v for k, v in op["set"].items()})
        return hashlib.sha1(json.dumps([p["kind"], p["key"], sets], sort_keys=True).encode()).hexdigest()[:16]
    con = db.connect()
    with con:
        for i, p in enumerate(props):
            q = dict(p, fingerprint=old_fp(p))
            status = "applied" if i == 0 else ("superseded" if i == 1 else "pending")
            con.execute("INSERT INTO proposals (fingerprint, kind, title, payload, status, first_seen, last_seen) "
                        "VALUES (?,?,?,?,?,?,?)", (q["fingerprint"], q["kind"], q["title"], json.dumps(q), status, "t", "t"))
    con.close()
    s = db.record_run(*build_proposals(locs, accs, cons, today="2026-11-06"), {"mode": "test"})
    assert s["new"] == 0 and s["superseded"] == 0, s
    assert s["already_decided_skipped"] == 1 and s["still_pending"] == 27, s
    print("old database migrated: decisions kept, wrongly superseded item restored:", s)


if __name__ == "__main__":
    main()
    test_fingerprints_stable_across_days_and_renames()
    test_migrates_old_fingerprints()
    test_rules_are_tunable()
    test_reviewer_note_written()
    test_scorecard_before_after()
    test_verification_drafts()
    test_dues_cleared_turns_chow_into_reparent()
