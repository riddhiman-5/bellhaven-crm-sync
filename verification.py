"""Drafts a verification email for a proposal. Nothing is sent automatically: the reviewer
edits the draft, sends it from their own email, and records the reply in the app.
The reply becomes evidence on the proposal; the reviewer still makes the final decision.
"""

SIGNOFF = "\n\nThank you,\n[Your name]\nAccount Data Team\n[Your phone / email]"


def _money_line(a):
    rev, ar = a.get("lifetime_revenue") or 0, a.get("outstanding_ar") or 0
    if ar > 0:
        return (f"\n\nOur records also show an outstanding balance of ${ar:,} on this account. "
                "Please confirm who should receive invoices for that balance (the previous owner or the new one), "
                "and whether it has been paid.")
    return ""


def draft(p):
    """Return (to, subject, body) for a proposal payload."""
    loc, accs, c = p.get("location"), p.get("accounts") or [], p.get("contact") or {}
    a = accs[0] if accs else {}
    name = (loc or {}).get("name") or a.get("name", "your facility")
    addr = (f"{loc['street']}, {loc['city']}, {loc['state']} {loc['zip']}" if loc
            else f"{a.get('billing_street')}, {a.get('billing_city')}, {a.get('billing_state')} {a.get('billing_zip')}")
    greet_name = c.get("name") or (loc or {}).get("administrator") or "Administrator"
    to = c.get("email") or ""
    hello = f"Hello {greet_name},\n\nWe're updating our records and would like to confirm a few details about {name} at {addr}."
    k = p["kind"]

    if k == "CHOW":
        body = (f"{hello}\n\nOur records list this facility under {a.get('parent_name', '').replace(' (Parent Account)', '')}, "
                "but Bellhaven Senior Living's website now lists it as a Bellhaven community.\n\n"
                "Could you confirm:\n1. Is the facility now owned/operated by Bellhaven Senior Living? (YES / NO)\n"
                "2. When did the change of ownership take effect?\n3. Who is the right contact at the new owner for contracts?"
                + _money_line(a))
        subject = f"Please confirm: change of ownership for {name}"
    elif k == "CHOW_LINK":
        other = accs[1] if len(accs) > 1 else {}
        body = (f"{hello}\n\nWe understand the facility may now be operated as {other.get('name', 'a different operator')} "
                f"({other.get('parent_name', '').replace(' (Parent Account)', '')}).\n\n"
                "Could you confirm:\n1. Has ownership changed? (YES / NO)\n2. When did it take effect?" + _money_line(a))
        subject = f"Please confirm: ownership of {a.get('name', name)}"
    elif k == "REPARENT":
        body = (f"{hello}\n\nOur records list this facility under "
                f"{(a.get('parent_name') or 'no parent company').replace(' (Parent Account)', '')}. "
                "Bellhaven Senior Living's website lists it as one of their communities.\n\n"
                "Could you confirm the facility is owned/operated by Bellhaven Senior Living? (YES / NO)")
        subject = f"Please confirm: parent company for {name}"
    elif k == "UPDATE":
        diffs = "\n".join(f"- {ch['field']}: we have '{ch['before']}', website shows '{ch['after']}'" for ch in p["changes"])
        body = f"{hello}\n\nWe found these differences:\n{diffs}\n\nCould you confirm the website version is correct? (YES / NO)"
        subject = f"Please confirm: updated details for {name}"
    elif k == "DUPLICATE":
        names = " and ".join(f"'{x['name']}'" for x in accs)
        body = (f"{hello}\n\nWe have more than one record for this address ({names}). "
                f"Could you confirm these are the same single facility, currently called '{name}'? (YES / NO)")
        subject = f"Please confirm: one facility at {addr}"
    elif k == "CREATE":
        offers = ", ".join(loc.get("care_offerings") or [])
        body = (f"{hello}\n\nWe'd like to add your community to our records with these details:\n"
                f"- Name: {name}\n- Address: {addr}\n- Care offered: {offers}\n- Phone: {loc.get('phone')}\n"
                "- Operator: Bellhaven Senior Living\n\nAre these correct? (YES / NO, with any corrections)")
        subject = f"Please confirm: details for {name}"
        greet_name = loc.get("administrator")
    else:  # STALE
        body = (f"{hello}\n\nThis facility is no longer listed on Bellhaven Senior Living's website.\n\n"
                "Could you tell us:\n1. Is the facility still operating? (YES / NO)\n"
                "2. If so, who owns/operates it now?\n3. Who is the best contact going forward?" + _money_line(a))
        subject = f"Please confirm: current status of {name}"
    return to, subject, body + SIGNOFF
