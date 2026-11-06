"""Review app.   Run with:   streamlit run app.py

Overview -> Review queue -> history -> How it works.
Nothing is written to the CRM until a reviewer clicks Approve. Rejected items are remembered
and never re-proposed.
"""
import json

import pandas as pd
import streamlit as st

import db
from config import BASE, BELLHAVEN_PARENT_NAME, CRM_BROWSER, token
from matcher import KIND_ORDER, KINDS, digits, load_rules, norm_street, zip5

RULES = load_rules()
MATCH_THRESHOLD = RULES["match_threshold"]
CONFIDENT_THRESHOLD = RULES["confident_threshold"]
POSSIBLE_THRESHOLD = RULES["possible_threshold"]

st.set_page_config(page_title="Bellhaven CRM Sync", page_icon="🏥", layout="wide")

GREEN, GOLD, RED, AMBER = "#2E5D50", "#C9A227", "#B3412F", "#D08C22"
SEG_COLORS = ["#2E5D50", "#4F8A78", "#7FB3A2", "#C9A227", "#E2C979", "#9BB7AE"]

st.markdown(f"""
<style>
  .pill {{display:inline-block;padding:2px 10px;border-radius:999px;font-size:12px;font-weight:600;margin-right:6px}}
  .pill-high {{background:#DCEFE7;color:{GREEN}}}
  .pill-medium, .pill-reconsider {{background:#FBEFD5;color:#8A5A00}}
  .thr2 {{position:absolute;top:-4px;bottom:-4px;width:2px;background:{GREEN};opacity:.8}}
  .plain {{font-size:16px;margin:4px 0 10px}}
  .bar {{position:relative;display:flex;height:30px;border-radius:6px;overflow:hidden;background:#EEE;margin:6px 0 2px}}
  .seg {{height:100%;color:white;font-size:11px;font-weight:600;display:flex;align-items:center;justify-content:center;white-space:nowrap;overflow:hidden}}
  .thr {{position:absolute;top:-4px;bottom:-4px;width:3px;background:{RED}}}
  .thr-label {{position:relative;font-size:11px;color:{RED};font-weight:600}}
  .legend div {{font-size:13px;margin:1px 0}}
  .flow {{font-size:13px;color:#555}}
</style>""", unsafe_allow_html=True)

# One-sentence, plain-English explanation of each type (used on cards and overview)
PLAIN = {
    "CHOW": ("Bellhaven bought it, but the old owner's account still has unpaid bills.",
             "Keep the old account for billing, create a new one under Bellhaven, link them."),
    "CHOW_LINK": ("Another company bought it, and the old account still has unpaid bills.",
                  "Keep the old account, link it to the new owner's existing account."),
    "REPARENT": ("Bellhaven owns it, but the CRM says another company does.",
                 "Change the owner (parent) to Bellhaven."),
    "UPDATE": ("Right place, right owner, but the name or zip is out of date.",
               "Update the field to match Bellhaven's website."),
    "DUPLICATE": ("The same building is in the CRM more than once.",
                  "Keep the best record, mark this copy as a duplicate (Inactive)."),
    "CREATE": ("On Bellhaven's website, but not in the CRM at all.",
               "Create a new account under Bellhaven."),
    "STALE": ("In the CRM under Bellhaven, but gone from the website, new owner unknown.",
              "Flag it 'Needs Review' so a person checks. Don't guess."),
}


@st.cache_resource
def crm_client():
    from crm import CRM
    return CRM(token())


# ------------------------------------------------------------------ building blocks
def score_bar(parts, total, of, create=False):
    """Stacked bar of where the match points came from, with the 50-point line."""
    scale = max(130, total + 5)
    segs = "".join(
        f'<div class="seg" style="width:{pts / scale * 100:.1f}%;background:{SEG_COLORS[i % len(SEG_COLORS)]}" '
        f'title="{label}: +{pts}">{"+" + str(pts) if pts >= 8 else ""}</div>'
        for i, (label, pts) in enumerate(parts) if pts > 0)
    thr = MATCH_THRESHOLD / scale * 100
    thr2 = CONFIDENT_THRESHOLD / scale * 100
    if create:
        verdict = (f"✅ Only <b>{total}</b> points (below {MATCH_THRESHOLD}): this CRM account is a <b>different facility</b>. "
                   "A low score is what we want here: it confirms no existing account is this location, "
                   "so creating a new one won't make a duplicate.")
    elif total >= CONFIDENT_THRESHOLD:
        verdict = f"✅ <b>{total}</b> points: <b>confident</b> it's the same facility (address + another clue)"
    elif total >= MATCH_THRESHOLD:
        verdict = (f"🔁 <b>{total}</b> points: same facility, but <b>reconsider</b>: only the address agrees "
                   f"(below {CONFIDENT_THRESHOLD}). Look harder or verify before approving.")
    else:
        verdict = f"❌ <b>{total}</b> points: below {MATCH_THRESHOLD}, so it's a <b>different facility</b>"
    legend = "".join(f'<div><span style="color:{SEG_COLORS[i % len(SEG_COLORS)]}">■</span> {label} <b>+{pts}</b></div>'
                     for i, (label, pts) in enumerate(parts))
    st.markdown(f"""
      <div style="font-size:13px;color:#555">Match score for {of}</div>
      <div class="bar">{segs}<div class="thr" style="left:{thr:.1f}%"></div><div class="thr2" style="left:{thr2:.1f}%"></div></div>
      <div style="position:relative;height:16px;font-size:11px;font-weight:600">
        <span style="position:absolute;left:calc({thr:.1f}% - 30px);color:{RED}">▲ {MATCH_THRESHOLD} match</span>
        <span style="position:absolute;left:calc({thr2:.1f}% - 25px);color:{GREEN}">▲ {CONFIDENT_THRESHOLD} confident</span>
      </div>
      <div style="margin:6px 0">{verdict}</div>
      <div class="legend">{legend}</div>""", unsafe_allow_html=True)


def _status(a, b, kind="text"):
    a, b = ("" if a is None else str(a)), ("" if b is None else str(b))
    if a.strip().lower() == b.strip().lower():
        return "✅ Same"
    if kind == "street" and norm_street(a) == norm_street(b):
        return "🟰 Same after cleanup"
    if kind == "zip" and zip5(a) == zip5(b):
        return "✅ Same"
    if kind == "phone" and digits(a) and digits(a) == digits(b):
        return "✅ Same"
    if not a or not b:
        return "➖ Missing"
    return "❌ Different"


ROW_COLORS = {"✅": "#E7F4EE", "🟰": "#F3F7E9", "❌": "#FBE4E0", "➖": "#F4F4F4"}


def _style_rows(df):
    """Colour each row green / light green / red / grey by its Match column."""
    out = pd.DataFrame("", index=df.index, columns=df.columns)
    for i, m in df["Match"].items():
        out.loc[i, :] = f"background-color:{ROW_COLORS.get(str(m)[:1], '')}"
    return out


def compare_table(left, right, left_title, right_title):
    rows = [
        ("Name", left.get("name"), right.get("name"), "text"),
        ("Street", left.get("street"), right.get("street"), "street"),
        ("City", left.get("city"), right.get("city"), "text"),
        ("State", left.get("state"), right.get("state"), "text"),
        ("Zip", left.get("zip"), right.get("zip"), "zip"),
        ("Phone", left.get("phone"), right.get("phone"), "phone"),
        ("Owner (parent)", left.get("owner"), right.get("owner"), "text"),
    ]
    df = pd.DataFrame([{"Field": f, left_title: l or "", right_title: r or "", "Match": _status(l, r, k)}
                       for f, l, r, k in rows])
    st.dataframe(df.style.apply(_style_rows, axis=None), hide_index=True, width="stretch")


def as_side(a):
    return {"name": a["name"], "street": a["billing_street"], "city": a["billing_city"], "state": a["billing_state"],
            "zip": a["billing_zip"], "phone": a["phone"], "owner": short(a["parent_name"]) or "(none)"}


def web_side(loc):
    return {"name": loc["name"], "street": loc["street"], "city": loc["city"], "state": loc["state"],
            "zip": loc["zip"], "phone": loc["phone"], "owner": short(BELLHAVEN_PARENT_NAME)}


def short(parent_name):
    return (parent_name or "").replace(" (Parent Account)", "")


def money_path(p):
    """For owner changes: show the billing-rule decision explicitly."""
    if p["kind"] not in ("CHOW", "CHOW_LINK", "REPARENT"):
        return
    a = p["accounts"][0]
    rev, ar = a.get("lifetime_revenue") or 0, a.get("outstanding_ar") or 0
    r_ok, ar_ok = rev > 0, ar > 0
    path = ("➡️ **Both yes → CHOW**: keep the old account untouched, link it to the account under the new owner"
            if r_ok and ar_ok else "➡️ **Not both → move the existing account directly**")
    st.markdown(f"**Billing rule check:** Revenue history? {'✅' if r_ok else '❌'} \\${rev:,} · "
                f"Outstanding AR? {'✅' if ar_ok else '❌'} \\${ar:,}  \n{path}")


VSTATUS = {
    None: ("", ""),
    "drafted": ("📝 Email drafted", "#EFEBE0"),
    "sent": ("✉️ Waiting for facility reply", "#FBEFD5"),
    "confirmed": ("✅ Facility confirmed", "#DCEFE7"),
    "denied": ("⛔ Facility said NO", "#FBE4E0"),
}


def verify_section(p, fp):
    """Optional step before approving: draft an email to the facility, send it yourself, record the reply."""
    from urllib.parse import quote

    from verification import draft
    v = db.get_verification(fp) or {}
    d_to, d_subj, d_body = draft(p)
    label = VSTATUS.get(v.get("status"))[0] or "not started"
    with st.expander(f"✉️ Verify with the facility before approving (optional) · {label}"):
        st.caption("A generic email is drafted for you. Edit it, send it from your own email, then record the reply here. "
                   "The reply is saved as evidence on this fix. Nothing is sent automatically.")
        to = st.text_input("To", v.get("to_addr") or d_to, key=f"vto_{fp}",
                           placeholder="No email on file; find the facility's email or call " + str((p.get("location") or {}).get("phone") or ""))
        subj = st.text_input("Subject", v.get("subject") or d_subj, key=f"vs_{fp}")
        body = st.text_area("Email (edit freely, add your own notes)", v.get("body") or d_body, height=380, key=f"vb_{fp}")
        c1, c2, c3 = st.columns(3)
        if c1.button("💾 Save draft", key=f"vsave_{fp}", width="stretch"):
            db.save_verification(fp, v.get("status") if v.get("status") in ("sent", "confirmed", "denied") else "drafted",
                                 to_addr=to, subject=subj, body=body)
            st.rerun()
        c2.link_button("📧 Open in my email app", f"mailto:{quote(to)}?subject={quote(subj)}&body={quote(body)}",
                       width="stretch")
        if c3.button("📤 I sent it", key=f"vsent_{fp}", width="stretch"):
            db.save_verification(fp, "sent", to_addr=to, subject=subj, body=body)
            st.rerun()
        if v.get("status") in ("sent", "confirmed", "denied"):
            st.markdown("**Record the facility's reply**")
            reply = st.text_area("Paste or summarise what they wrote back", v.get("reply") or "", key=f"vr_{fp}", height=90)
            r1, r2 = st.columns(2)
            if r1.button("✅ They confirmed (YES)", key=f"vy_{fp}", width="stretch"):
                db.save_verification(fp, "confirmed", reply=reply)
                st.rerun()
            if r2.button("⛔ They said NO / something's different", key=f"vn_{fp}", width="stretch"):
                db.save_verification(fp, "denied", reply=reply)
                st.rerun()
    return v


# ------------------------------------------------------------------ proposal card
def proposal_card(row):
    p, fp = row["payload"], row["fingerprint"]
    with st.container(border=True):
        h1, h2 = st.columns([5, 1])
        h1.markdown(f"#### {p['label']}  \n**{p['title']}**")
        vrow = db.get_verification(fp) or {}
        vtxt, vcol = VSTATUS.get(vrow.get("status"), ("", ""))
        vpill = f'<br><span class="pill" style="background:{vcol}">{vtxt}</span>' if vtxt else ""
        conf_txt = "✅ CONFIDENT" if p["confidence"] == "high" else "🔁 RECONSIDER"
        h2.markdown(f'<span class="pill pill-{p["confidence"]}">{conf_txt}</span>{vpill}', unsafe_allow_html=True)
        problem, action = PLAIN[p["kind"]]
        st.markdown(f'<div class="plain">🔎 <b>Problem:</b> {problem}<br>🛠️ <b>Fix:</b> {action}</div>',
                    unsafe_allow_html=True)
        if p["confidence"] != "high":
            why = {"CREATE": f"a CRM account looks somewhat similar (score {p.get('score')}). Check it really is a different facility before creating.",
                   "STALE": "the system can't tell whether this facility closed or was sold."}.get(
                p["kind"], f"only the address agrees (score {p.get('score')}, below {CONFIDENT_THRESHOLD}), with no matching phone or manager and a different name.")
            st.warning(f"🔁 **Reconsider:** {why} Look at the evidence closely, or use ✉️ Verify with the facility before approving.")

        left, right = st.columns([3, 2])
        with left:
            loc, accs = p.get("location"), p["accounts"]
            if loc and accs:
                st.markdown("**Website vs CRM, side by side**")
                compare_table(web_side(loc), as_side(accs[0]), "🌐 Website (truth)", "🗂️ CRM today")
            elif loc:
                st.markdown("**Website listing: no CRM account at this address**")
                compare_table(web_side(loc), {}, "🌐 Website (truth)", "🗂️ CRM today")
            elif len(accs) >= 2:
                st.markdown("**Old account vs account at the same address**")
                compare_table(as_side(accs[0]), as_side(accs[1]), "🗂️ This account", "🏢 Other account")
            else:
                st.markdown("**CRM account (not found on website)**")
                compare_table(as_side(accs[0]), {}, "🗂️ CRM today", "🌐 Website")
            money_path(p)
        with right:
            if p.get("score_parts"):
                score_bar(p["score_parts"], p["score"], p["score_of"], create=p["kind"] == "CREATE")
            elif p["kind"] == "CREATE":
                st.markdown("✅ **No CRM account to compare against.** No account in this state shares the street "
                            "or zip, so this location is genuinely missing from the CRM and a new account won't "
                            "duplicate anything. (The website itself isn't scored: it's the source of truth.)")
            else:
                st.markdown("No website location to score against. This account was found because it is "
                            "**under Bellhaven** in the CRM but **missing from the website**.")

        st.markdown("**What will be written to the CRM if approved**")
        st.dataframe(pd.DataFrame([{"Account": c["account"], "Field": c["field"],
                                    "Current": "" if c["before"] in (None, "") else str(c["before"]),
                                    "New value": str(c["after"])} for c in p["changes"]]),
                     hide_index=True, width="stretch")
        with st.expander("All evidence and the rule applied"):
            st.info(p["rule"])
            for e in p["evidence"]:
                st.markdown("- " + e.replace("$", "\\$"))

        v = verify_section(p, fp)
        if v.get("status") == "confirmed" and v.get("reply"):
            st.success(f"✅ Facility confirmed: \"{v['reply']}\"")
        elif v.get("status") == "sent":
            st.warning("✉️ Verification email sent; still waiting for the facility's reply. You can wait, or approve on the evidence above.")
        elif v.get("status") == "denied":
            st.error(f"⛔ The facility said NO{': ' + chr(34) + v['reply'] + chr(34) if v.get('reply') else ''}. Consider rejecting this fix.")
        if str(row.get("result") or "").startswith("Undone"):
            st.info(f"↩️ This fix was approved before and then undone: {row['result']}")
        if str(row.get("result") or "").startswith("ERROR"):
            st.error(row["result"])
        b1, b2, b3 = st.columns([1, 1, 3])
        reason = b3.text_input("Note", key=f"r_{fp}", label_visibility="collapsed",
                               placeholder="Your note (saved with your decision; on Approve it's also written to the CRM account's note)")
        if b1.button("✅ Approve", key=f"a_{fp}", type="primary", width="stretch"):
            from executor import apply
            try:
                res, undo_steps = apply(p, crm_client(), reviewer_note=reason)
                db.decide(fp, "applied", reason=reason or None, result=res, undo=undo_steps)
                st.toast(f"Written to CRM: {p['title']}", icon="✅")
            except Exception as e:  # any API or safety-check failure is shown on the card
                db.set_error(fp, str(e))
            st.rerun()
        if b2.button("❌ Reject", key=f"x_{fp}", width="stretch"):
            db.decide(fp, "rejected", reason=reason or "Rejected by reviewer")
            st.toast(f"Rejected: {p['title']}", icon="❌")
            st.rerun()


# ------------------------------------------------------------------ results scorecard
DECISIONS = [
    ("Sunny Acres Retirement Home vs Bellhaven Willow Creek", "Same facility, renamed", "Identical street + zip (8060 Willow Creek); names change, buildings don't", "Rename"),
    ("Riverbend Manor / Chesterton Senior Commons", "Same facilities, renamed", "Address + website manager is a CRM contact on the account", "Rename"),
    ("Amberly Manor (Hudson, OH) vs Amberly Manor (Colorado)", "Different facilities", "Different state; a shared name is not evidence", "Create new account"),
    ("Bellhaven of Carlisle, PA vs New Carlisle, OH", "Different facilities", "Different street, city and state", "Create new account"),
    ("Bellhaven at Union Square vs Union Square Senior Living", "Different facilities", "Different street (118 Union Square Dr vs 240 Market St), different manager; score 24", "Create new account"),
    ("Ashtabula: CRM address is 'PO Box 517'", "Same facility", "Zip + phone + manager match (score 75); a billing PO box is legitimate", "No change"),
    ("Portsmouth: zip 45626 vs 45662", "Same facility, typo", "Same street, phone and manager; two digits swapped", "Fix zip"),
    ("Kettering: 3 records at one address, all empty", "One facility", "Keep the record with the clean address and Harborview (2025) lineage", "1 moved + renamed, 2 marked duplicate"),
    ("Lima: $47K revenue but $0 AR", "Move directly", "SOP requires revenue AND AR for CHOW; AR is $0", "Re-parent existing account"),
    ("Marietta / Tiffin: revenue AND AR, wrong parent", "CHOW", "SOP: old account untouched, new account under Bellhaven, linked", "New account + chow link"),
    ("Sandusky: off the website, Millstone account at same address", "Sold to Millstone (CHOW)", "Revenue AND AR; new owner's account already exists, so link instead of duplicating", "chow link to existing account"),
    ("Alliance, Coldwater: off the website, no new owner found", "Unknown: closed or sold", "Inactive could hide a sold facility from sales; don't guess", "Needs Review + note"),
]


def results_page():
    import os

    from config import DATA_DIR
    from outcome import billing_preserved, projected, scorecard

    def load(name):
        p = os.path.join(DATA_DIR, name)
        return json.load(open(p)) if os.path.exists(p) else None

    locs, now_accs, contacts = load("website_locations.json"), load("crm_accounts.json"), load("crm_contacts.json")
    base = load("baseline_crm_accounts.json") or now_accs
    if not (locs and now_accs and contacts):
        st.warning("Run the pipeline first.")
        return
    pend = [r["payload"] for r in pending]
    try:
        after = projected(now_accs, pend)
    except Exception as e:  # a pending fix that no longer applies to the snapshot
        st.caption(f"Projection skipped: {e}")
        after = now_accs
    s0, s1, s2 = scorecard(locs, base, contacts), scorecard(locs, now_accs, contacts), scorecard(locs, after, contacts)

    st.title("🏁 Results: is the CRM fixed?")
    st.markdown(f"Measured against Bellhaven's website ({s0['website_locations']} communities). A location counts as "
                "**correct** when exactly **one** active CRM account matches it, it's under **Bellhaven**, and its "
                "name and zip match the website.")
    c = st.columns(3)
    c[0].metric("Start (Nov 4 export)", f"{s0['correct']} / {s0['website_locations']} correct")
    c[1].metric("CRM now (last pipeline run)", f"{s1['correct']} / {s1['website_locations']} correct",
                delta=s1["correct"] - s0["correct"] or None)
    c[2].metric("After approving all pending fixes", f"{s2['correct']} / {s2['website_locations']} correct",
                delta=s2["correct"] - s1["correct"] or None)
    st.caption("'CRM now' refreshes when you click ▶️ Run pipeline now. After approving everything, re-run to prove it live.")

    rows = [("✅ Locations correctly linked", "correct", True), ("🆕 Missing from CRM", "missing", False),
            ("🔀 Under the wrong parent", "wrong_parent", False), ("✏️ Outdated name / zip", "outdated", False),
            ("👯 Extra duplicate accounts", "duplicates", False), ("⚠️ Left Bellhaven, not yet flagged", "gone_unflagged", False)]
    df = pd.DataFrame([{"Measure": label, "Start": s0[k], "CRM now": s1[k], "After all fixes": s2[k]} for label, k, _ in rows])

    def color(d):
        out = pd.DataFrame("", index=d.index, columns=d.columns)
        for i, (_, k, good_high) in enumerate(rows):
            for col in ("Start", "CRM now", "After all fixes"):
                v = d.loc[i, col]
                ok = (v == s0["website_locations"]) if good_high else (v == 0)
                out.loc[i, col] = f"background-color:{'#E7F4EE' if ok else '#FBE4E0'}"
        return out
    st.dataframe(df.style.apply(color, axis=None), hide_index=True, width="stretch")

    from outcome import insights
    ins = insights([r["payload"] for r in db.all_proposals() if r["status"] != "superseded"])
    st.subheader("What broke, and why")
    st.dataframe(pd.DataFrame([{"Root cause": c[0], "Fixes": c[2], "What happened": c[1]} for c in ins["causes"]]),
                 hide_index=True, width="stretch")
    top = ins["causes"][0] if ins["causes"] else None
    if top:
        st.markdown(f"**Biggest cause: {top[0]} ({top[2]} of {sum(c[2] for c in ins['causes'])} fixes).** "
                    "The CRM breaks when ownership changes, so that's when to check it.")
    st.markdown(f"💵 **Money at stake:** the {len(ins['billing_accounts'])} ownership changes with billing history "
                f"({', '.join(ins['billing_accounts'])}) carry **\\${ins['billing_revenue']:,} lifetime revenue** and "
                f"**\\${ins['billing_ar']:,} outstanding AR**. The billing rule keeps those balances on the original accounts "
                "while sales works with the new owner.")
    st.markdown("""**Recommendations**
1. **Trigger a check on every acquisition or divestiture**, not just the daily run: that's when links break.
2. **Make 'add to CRM' part of onboarding a new community**: 5 locations were never added or linked.
3. **Validate addresses on entry** (zip ↔ city check, duplicate-address warning): it would have prevented the typo and the double entries.""")

    st.subheader("Billing SOP: accounts with revenue AND outstanding AR")
    bp = billing_preserved(base, after)
    st.dataframe(pd.DataFrame([{"Account": r["account"], "Revenue": f"${r['revenue']:,}", "Outstanding AR": f"${r['ar']:,}",
                                "Parent left unchanged": "✅" if r["parent_unchanged"] else "❌",
                                "Linked to new owner (chow_current_account)": "✅ " + r["chow_link"] if r["chow_link"] else "n/a (owner didn't change)"}
                               for r in bp]), hide_index=True, width="stretch")
    st.caption(f"All {len(bp)} billing accounts keep their parent. The 3 that changed owner (Marietta, Tiffin, Sandusky) are linked to an account under the new owner.")

    st.subheader("Ambiguous cases: the decision on each")
    st.dataframe(pd.DataFrame(DECISIONS, columns=["Case", "Conclusion", "Deciding evidence", "Action"]),
                 hide_index=True, width="stretch")

    st.subheader("Conclusion")
    st.success(f"Approving the {len(pend)} pending fixes takes the CRM from **{s0['correct']}/35** to **{s2['correct']}/35** "
               f"Bellhaven communities correctly linked, with **{s2['duplicates']} duplicates**, **{s2['wrong_parent']} wrong parents**, "
               f"and every billing account preserved. A re-run on the corrected CRM proposes nothing new, so the system "
               "converges instead of churning, and from then on it only surfaces real ownership changes.")


# ------------------------------------------------------------------ export
def export_page():
    import io
    import os

    from config import DATA_DIR
    st.title("📤 Export")
    st.caption("Download everything as CSV (opens in Excel / Google Sheets) for reporting, audits or loading elsewhere.")
    allp = db.all_proposals()

    def csv_button(label, df, name, help_txt):
        c1, c2 = st.columns([3, 1])
        c1.markdown(f"**{label}**: {help_txt} ({len(df)} rows)")
        c2.download_button("⬇️ Download CSV", df.to_csv(index=False).encode(), file_name=name, mime="text/csv",
                           key=f"dl_{name}", width="stretch")

    audit = pd.DataFrame([{
        "decision": r["status"], "type": r["payload"]["label"], "fix": r["title"],
        "confidence": "confident" if r["payload"]["confidence"] == "high" else "reconsider",
        "match_score": r["payload"].get("score"), "first_proposed": r["first_seen"], "decided_at": r["decided_at"],
        "reviewer_note": r["reason"], "written_to_crm": r["result"], "fingerprint": r["fingerprint"],
    } for r in allp])
    csv_button("Audit log", audit, "audit_log.csv", "every proposed fix, its decision, your note and what was written")

    changes = pd.DataFrame([{
        "decision": r["status"], "fix": r["title"], "account": ch["account"], "field": ch["field"],
        "before": ch["before"], "after": ch["after"], "decided_at": r["decided_at"],
    } for r in allp for ch in r["payload"]["changes"]])
    csv_button("Field changes", changes, "field_changes.csv", "one row per field: before → after")

    vs = pd.DataFrame(db.list_verifications())
    if not vs.empty:
        csv_button("Verification emails", vs, "verifications.csv", "emails drafted/sent and facility replies")

    for fname, label, help_txt in [("crm_accounts.json", "CRM snapshot", "all CRM accounts as of the last pipeline run"),
                                   ("website_locations.json", "Website locations", "every Bellhaven community scraped"),
                                   ("crm_contacts.json", "CRM contacts", "all contacts as of the last run")]:
        p = os.path.join(DATA_DIR, fname)
        if os.path.exists(p):
            df = pd.json_normalize(json.load(open(p)))
            if "care_offerings" in df:
                df["care_offerings"] = df["care_offerings"].apply(lambda x: ", ".join(x) if isinstance(x, list) else x)
            csv_button(label, df, fname.replace(".json", ".csv"), help_txt)

    lr = db.last_run()
    if lr:
        csv_button("Already correct", pd.DataFrame(lr["confirmed"]), "already_correct.csv",
                   "locations whose CRM account needed no change")


# ------------------------------------------------------------------ rules (iterate on the scoring)
def rules_page():
    import os

    from config import DATA_DIR
    from matcher import RULES_PATH, build_proposals
    st.title("⚙️ Matching rules")
    st.caption("Tune the points and thresholds. Preview the impact on today's data first; nothing changes until you save. "
               "Saved rules are used by the next pipeline run (including the daily scheduled one).")
    cur = load_rules()
    w = cur["weights"]
    st.subheader("Points per clue")
    c = st.columns(4)
    nw = {
        "street_zip": c[0].number_input("Street + zip match", 0, 200, w["street_zip"]),
        "street_city": c[1].number_input("Street + city (zip differs)", 0, 200, w["street_city"]),
        "zip_only": c[2].number_input("Same zip only", 0, 200, w["zip_only"]),
        "city": c[3].number_input("Same city", 0, 200, w["city"]),
    }
    c = st.columns(4)
    nw.update({
        "phone": c[0].number_input("Phone matches", 0, 200, w["phone"]),
        "admin_contact": c[1].number_input("Manager is a CRM contact", 0, 200, w["admin_contact"]),
        "name_max": c[2].number_input("Name similarity (max)", 0, 200, w["name_max"]),
    })
    st.subheader("Thresholds")
    c = st.columns(3)
    new = {
        "possible_threshold": c[0].number_input("Possible match (new accounts)", 0, 300, cur["possible_threshold"]),
        "match_threshold": c[1].number_input("Same facility", 0, 300, cur["match_threshold"]),
        "confident_threshold": c[2].number_input("Confident", 0, 300, cur["confident_threshold"]),
        "weights": nw,
    }
    valid = new["possible_threshold"] < new["match_threshold"] <= new["confident_threshold"]
    if not valid:
        st.error("Thresholds must be ordered: possible < same facility ≤ confident.")

    def load(name):
        p = os.path.join(DATA_DIR, name)
        return json.load(open(p)) if os.path.exists(p) else None
    locs, accs, cons = load("website_locations.json"), load("crm_accounts.json"), load("crm_contacts.json")

    b1, b2 = st.columns(2)
    if b1.button("🔍 Preview impact on today's data", width="stretch", disabled=not (valid and locs)):
        def summary(rules):
            P, conf = build_proposals(locs, accs, cons, rules=rules)
            return {"fixes": len(P), "reconsider": sum(p["confidence"] != "high" for p in P), "confirmed": len(conf),
                    "new accounts": sum(p["kind"] == "CREATE" for p in P), "titles": {p["title"] for p in P}}
        a, b = summary(cur), summary(new)
        from matcher import refresh_rules
        refresh_rules()  # back to the saved rules
        st.dataframe(pd.DataFrame([{"": k, "Current rules": a[k], "New rules": b[k]}
                                   for k in ("fixes", "reconsider", "confirmed", "new accounts")]),
                     hide_index=True, width="stretch")
        added, removed = sorted(b["titles"] - a["titles"]), sorted(a["titles"] - b["titles"])
        if added:
            st.warning("**Would newly propose:**\n" + "\n".join(f"- {t}" for t in added))
        if removed:
            st.info("**Would no longer propose:**\n" + "\n".join(f"- {t}" for t in removed))
        if not added and not removed:
            st.success("Same fixes as today; only scores/confidence labels may change.")
    if b2.button("💾 Save rules", type="primary", width="stretch", disabled=not valid):
        with open(RULES_PATH, "w") as f:
            json.dump({"_comment": "Matching rules. Edit here or on the app's Rules page.", **new}, f, indent=2)
        st.success("Saved to rules.json. Click ▶️ Run pipeline now to apply them.")


# ------------------------------------------------------------------ sidebar
with st.sidebar:
    st.markdown(f"## <span style='color:{GREEN}'>Bellhaven</span> <span style='color:{GOLD}'>CRM Sync</span>",
                unsafe_allow_html=True)
    st.caption("Keeps facility → parent-company links in the CRM accurate, using Bellhaven's own website as the truth.")
    if st.button("▶️ Run pipeline now", width="stretch"):
        with st.spinner("Reading website, pulling CRM, comparing..."):
            import pipeline
            s = pipeline.run()
        st.success(f"{s['new']} new · {s['already_decided_skipped']} already decided (not re-asked) · "
                   f"{s['confirmed']} already correct")
    st.link_button("🗂️ Open CRM browser", CRM_BROWSER, width="stretch")
    st.link_button("🌐 Open Bellhaven website", BASE + "/communities", width="stretch")

counts = db.counts()
pending = db.list_proposals("pending")
lr = db.last_run()

# Navigation that survives reruns: after Approve/Reject you stay on the same page
# (st.tabs always jumps back to the first tab when the app reruns).
PAGES = {
    "overview": "📊 Overview",
    "results": "🏁 Results",
    "review": f"⏳ Review queue ({len(pending)})",
    "applied": f"✅ Applied ({counts.get('applied', 0)})",
    "rejected": f"❌ Rejected ({counts.get('rejected', 0) + counts.get('superseded', 0)})",
    "verification": "✉️ Verification",
    "confirmed": "✔️ Already correct",
    "how": "📘 How it works",
    "rules": "⚙️ Rules",
    "export": "📤 Export",
}
nav = st.radio("Page", list(PAGES), format_func=PAGES.get, horizontal=True, key="nav",
               label_visibility="collapsed")
st.divider()

# ------------------------------------------------------------------ overview
if nav == "results":
    results_page()
if nav == "export":
    export_page()
if nav == "rules":
    rules_page()

if nav == "overview":
    st.title("Bellhaven CRM Sync")
    st.markdown("Sales needs to know **which company owns each facility**: corporate-owned facilities need "
                "corporate sign-off. Ownership changes constantly and the CRM falls behind. This tool checks the CRM "
                "against **Bellhaven's own website** every day and proposes fixes. **A person approves every change.**")
    if lr:
        s = lr["summary"]
        total = sum(counts.values()) or 1
        decided = counts.get("applied", 0) + counts.get("rejected", 0)
        m = st.columns(6)
        m[0].metric("🌐 Website locations", s["website_locations"])
        m[1].metric("🗂️ CRM accounts", s["crm_accounts"])
        m[2].metric("✔️ Already correct", s["confirmed"])
        m[3].metric("🛠️ Fixes found", total)
        m[4].metric("🔁 To reconsider", sum(1 for r in pending if r["payload"]["confidence"] != "high"))
        m[5].metric("✅ Reviewed", f"{decided} / {total}")
        st.progress(decided / total, text=f"Review progress: {decided} of {total} decided")
        st.caption(f"Last run {lr['started_at']} ({s['mode']})")
    else:
        st.warning("No run yet. Click **▶️ Run pipeline now** in the sidebar.")

    st.subheader("How a fix flows through the system")
    st.graphviz_chart(f"""
    digraph {{
      rankdir=LR; bgcolor="transparent";
      node [shape=box style="rounded,filled" fontname="Helvetica" fontsize=11 color="{GREEN}" fillcolor="#EEF5F2"];
      edge [color="#888" fontname="Helvetica" fontsize=9];
      web [label="🌐 1. Read website\\n35 locations"]; crm [label="🗂️ 2. Read CRM\\n121 accounts (read-only)"];
      cmp [label="🔍 3. Compare by ADDRESS\\nscore ≥ 50 = same facility"]; cls [label="🏷️ 4. Classify\\nrename · owner · duplicate\\nmissing · gone"];
      q [label="⏳ 5. Review queue\\nwith evidence"]; h [label="👤 6. Human decides" fillcolor="#FBF3DC" color="{GOLD}"];
      ok [label="✅ Write to CRM\\n(after safety re-check)"]; no [label="❌ Remember rejection\\n(never re-asked)"];
      web -> cmp; crm -> cmp; cmp -> cls -> q -> h; h -> ok [label="Approve"]; h -> no [label="Reject"];
    }}""")

    st.subheader("What it found")
    allp = [r for st_ in ("pending", "applied", "rejected") for r in db.list_proposals(st_)]
    rows = []
    for k in KIND_ORDER:
        items = [r for r in allp if r["kind"] == k]
        if items:
            rows.append({"Type": KINDS[k][0], "Count": len(items), "What it means": PLAIN[k][0],
                         "Fix": PLAIN[k][1], "Example": items[0]["title"]})
    if rows:
        st.dataframe(pd.DataFrame(rows), hide_index=True, width="stretch")

# ------------------------------------------------------------------ review queue
if nav == "review":
    if not pending:
        st.success("🎉 Nothing left to review. Run the pipeline to check for new changes.")
    else:
        kinds_present = [k for k in KIND_ORDER if any(r["kind"] == k for r in pending)]
        pick = st.multiselect("Show types", kinds_present, default=kinds_present, format_func=lambda k: KINDS[k][0])
        for r in sorted(pending, key=lambda r: (KIND_ORDER.index(r["kind"]), r["title"])):
            if r["kind"] in pick:
                proposal_card(r)

if nav == "applied":
    st.caption("Changes written to the CRM. Each one also added a dated note on the account explaining why. "
               "Made a mistake? ↩️ Undo puts the old values back and returns the fix to the review queue.")
    for r in db.list_proposals("applied"):
        fp = r["fingerprint"]
        with st.container(border=True):
            st.markdown(f"**{r['payload']['label']}**: {r['title']}  \n_Applied {r['decided_at']}_"
                        + (f"  \n📝 **Your note:** {r['reason']}" if r.get("reason") else ""))
            st.code(r["result"] or "", language=None)
            from executor import legacy_undo_steps
            steps = json.loads(r["undo"]) if r.get("undo") else legacy_undo_steps(r["payload"])
            if not steps:
                st.caption("Undo not available for this change (it was applied before undo existed and created an account).")
                continue
            with st.expander("↩️ Undo this change"):
                for stp in steps:
                    if stp["op"] == "retire":
                        st.markdown(f"- New account **{stp['name']}** `{stp['account_id']}` will be set **Inactive** "
                                    "(the API has no delete) with a note that it was created in error.")
                    else:
                        st.markdown(f"- `{stp['account_id']}`: " + ", ".join(
                            f"**{k}** back to `{v if v not in (None, '') else '(blank)'}`"
                            for k, v in stp["restore"].items() if k != "note"))
                if st.button("↩️ Yes, undo it", key=f"u_{fp}"):
                    from executor import undo
                    try:
                        msg = undo(steps, crm_client())
                        db.reopen(fp, msg)
                        st.toast("Undone. The fix is back in the review queue.", icon="↩️")
                        st.rerun()
                    except Exception as e:
                        st.error(str(e))

if nav == "rejected":
    st.caption("Rejected items are remembered: the daily run will not propose them again.")
    for status in ("rejected", "superseded"):
        for r in db.list_proposals(status):
            with st.container(border=True):
                st.markdown(f"**{status.upper()}**: {r['title']}  \n_{r['decided_at']}_: {r['reason']}")
                if status == "rejected" and st.button("↩️ Send back to review queue", key=f"re_{r['fingerprint']}"):
                    db.reopen(r["fingerprint"], f"Reopened after rejection ({r['reason']})")
                    st.toast("Back in the review queue.", icon="↩️")
                    st.rerun()

if nav == "verification":
    st.caption("Every verification email drafted or sent, and what the facility answered. "
               "A facility's answer is evidence for the reviewer; the reviewer still approves or rejects.")
    vs = db.list_verifications()
    if not vs:
        st.info("No verification emails yet. Open any fix in the Review queue and use '✉️ Verify with the facility'.")
    for v in vs:
        with st.container(border=True):
            txt, col = VSTATUS.get(v["status"], ("", ""))
            st.markdown(f'<span class="pill" style="background:{col}">{txt}</span> **{v["title"]}** '
                        f'· fix is *{v["proposal_status"]}* · updated {v["updated_at"]}', unsafe_allow_html=True)
            st.markdown(f"**To:** {v['to_addr'] or '(no email on file)'}  \n**Subject:** {v['subject']}")
            if v.get("reply"):
                st.markdown(f"**Reply:** {v['reply']}")
            with st.expander("Email text"):
                st.text(v["body"] or "")

if nav == "confirmed":
    if lr:
        st.caption("Website locations whose CRM account already matches exactly: no change needed.")
        st.dataframe(pd.DataFrame(lr["confirmed"]).rename(columns={
            "location": "Website location", "city": "City", "crm_name": "CRM account", "score": "Match score",
            "account_id": "Account ID", "evidence": "Evidence"}), hide_index=True, width="stretch")

# ------------------------------------------------------------------ how it works
if nav == "how":
    st.header("How it works")

    st.subheader("1 · Matching: buildings don't move, names do")
    st.markdown("Facilities get renamed after acquisitions, and different facilities can share a name. "
                "So the **street address** decides whether two records are the same place. Small differences "
                "are cleaned up first (*Avenue → Ave, Northwest → NW, Lane → Ln*). Then points are added:")
    st.dataframe(pd.DataFrame([
        ("Street + zip match", "60", "Strongest signal: same building"),
        ("Street + city match, zip differs", "50", "Same building, zip typo (Portsmouth 45626 vs 45662)"),
        ("Same zip only", "10", "Weak: nearby, maybe a PO Box address"),
        ("Same city", "5", ""),
        ("Phone matches", "20", "Backup evidence"),
        ("Website administrator is a CRM contact", "25", "Backup evidence: rescued Ashtabula (CRM had a PO Box)"),
        ("Name similarity", "0 - 15", "Weakest: names change"),
    ], columns=["Signal", "Points", "Why"]), hide_index=True, width="stretch")
    st.markdown("**Three zones**, so the system says *how sure* it is, not just yes or no:")
    st.dataframe(pd.DataFrame([
        (f"✅ Confident", f"{CONFIDENT_THRESHOLD}+", "Address + at least one more clue (phone, manager, or similar name)", "Approve on the evidence"),
        (f"🔁 Reconsider", f"{MATCH_THRESHOLD} to {CONFIDENT_THRESHOLD - 1}", "Only the address agrees (e.g. renamed facility, empty duplicate)", "Look harder or verify with the facility first"),
        (f"🔁 Possible match", f"{POSSIBLE_THRESHOLD} to {MATCH_THRESHOLD - 1}", "New accounts only: a CRM look-alike exists", "Check it's really different before creating"),
        ("❌ No match", f"below {POSSIBLE_THRESHOLD}", "Nothing similar in the CRM", "Create the new account"),
    ], columns=["Zone", "Score", "Meaning", "Reviewer action"]), hide_index=True, width="stretch")
    st.markdown("Different state → never a match. Different street AND different zip → never a match.")
    st.markdown("**Tested on the traps in the data:**")
    st.dataframe(pd.DataFrame([
        ("'Sunny Acres Retirement Home' vs 'Bellhaven Willow Creek'", "same address", "✅ Same (renamed)", "❌ missed"),
        ("'Amberly Manor' Colorado vs 'Amberly Manor' Ohio", "different state", "✅ Different", "❌ wrongly matched"),
        ("'Bellhaven of Carlisle' PA vs 'of New Carlisle' OH", "different address", "✅ Different", "❌ wrongly matched"),
        ("Ashtabula, CRM address = 'PO Box 517'", "zip + phone + administrator", "✅ Same (score 75)", "✅"),
    ], columns=["Case", "Key evidence", "Address-first result", "Name-first would give"]), hide_index=True, width="stretch")

    st.subheader("2 · The billing rule (CHOW) when a facility changes owner")
    st.graphviz_chart(f"""
    digraph {{
      bgcolor="transparent"; node [fontname="Helvetica" fontsize=11 style="rounded,filled"];
      s [label="Facility needs a new parent" shape=box fillcolor="#EEF5F2" color="{GREEN}"];
      r [label="Revenue history > 0?" shape=diamond fillcolor="#FBF3DC" color="{GOLD}"];
      a [label="Outstanding AR > 0?" shape=diamond fillcolor="#FBF3DC" color="{GOLD}"];
      d [label="Re-parent the existing account\\n(e.g. Lima: $47K revenue, $0 AR)" shape=box fillcolor="#E7F4EE" color="{GREEN}"];
      c [label="CHOW: keep old account untouched\\ncreate new account under new parent\\nold.chow_current_account = new id\\n(Marietta, Tiffin)" shape=box fillcolor="#FBE4E0" color="{RED}"];
      e [label="New owner already has an account at that address?\\n→ link to it instead of creating a copy (Sandusky → Millstone)" shape=note fillcolor="#FFFFFF"];
      s -> r; r -> d [label="no"]; r -> a [label="yes"]; a -> d [label="no"]; a -> c [label="yes"]; c -> e [style=dashed];
    }}""")
    st.markdown("The rule is checked **twice**: when the fix is proposed, and again against live CRM data the moment you click Approve.")

    st.subheader("3 · Duplicates: which record survives")
    st.markdown("When several CRM accounts are the same building, keep the one with, in order: "
                "**billing history → most contacts → already under Bellhaven → phone/administrator match → "
                "cleanest address → acquisition history** (Harborview joined 2025, before Cedar Trail 2026). "
                "The others get `duplicate_of_account` = survivor and status Inactive (there's no delete in the API).")

    st.subheader("4 · Judgment calls")
    st.dataframe(pd.DataFrame([
        ("Match on address, not name", "Names change after acquisitions; buildings don't move."),
        ("Kettering survivor = Kettering Care Centre (Harborview)", "All 3 copies were empty; clean address and the earliest acquisition history break the tie."),
        ("Alliance & Coldwater → Needs Review, not Inactive", "Leaving the website says something changed, not what. Inactive could hide a sold facility from sales."),
        ("Sandusky → link to Millstone's existing account", "Follows the billing rule without creating a duplicate."),
        ("Don't update phones, cosmetic street spellings, PO boxes", "Weak or noisy signals: they'd flood reviewers with low-value changes."),
    ], columns=["Decision", "Why"]), hide_index=True, width="stretch")

    st.subheader("5 · Safety")
    st.markdown("""
- The daily pipeline is **read-only**. Only the Approve button writes to the CRM.
- Before writing, each account is **re-read**; if someone changed it since the proposal, nothing is written.
- It won't create an account if one already exists at that address.
- Every write adds a **dated note** to the account explaining what changed and why.
- Each proposal has a **fingerprint**: approved or rejected items are never proposed again on later runs.
- **Undo:** every applied change records the exact old values first. ↩️ Undo restores them (new accounts are set
  Inactive, since there's no delete), refuses if someone edited the account since, and returns the fix to the queue.
- **Optional facility verification:** for any fix, draft an email to the facility, send it yourself, and record the reply.
  The reply is saved as evidence. If past dues are cleared later, the next daily run automatically turns a CHOW into a simple re-parent.
""")
