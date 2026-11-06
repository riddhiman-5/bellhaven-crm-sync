# Bellhaven CRM Sync

Keeps the CRM's facility → parent-company links accurate by checking them every day against
the operator's own website, proposing fixes with evidence, and writing **only approved** changes
back to the CRM.

**Why it matters.** About 60% of LTC facilities are owned by a parent company, and ownership changes
constantly. When the CRM link goes stale, reps contact the wrong office and miss that a contract needs
corporate sign-off. This system catches those breaks daily instead of whenever someone notices.

```
Website ──► scraper.py ─┐
                        ├─► matcher.py ──► state.db (review queue) ──► app.py ──Approve──► executor.py ──► CRM API
CRM API ──► crm.py ─────┘        (read-only, runs daily)                (human)     (safety checks, then write)
```

## Run it

```bash
pip3 install -r requirements.txt
echo "YOUR_API_TOKEN" > token.txt        # git-ignored
python3 pipeline.py                      # scrape + match + queue proposals (read-only)
streamlit run app.py                     # review: approve / reject
python3 tests/test_end_to_end.py         # offline tests against an in-memory CRM
```

## Results

Measured against Bellhaven's website (35 communities). A location is **correct** when exactly one active CRM account
matches it, it sits under Bellhaven, and its name and zip match the website.

| Measure | Start (Nov 4 export) | After approved fixes |
|---|---|---|
| ✅ Locations correctly linked | **13 / 35** | **35 / 35** |
| 🆕 Missing from CRM | 4 | 0 |
| 🔀 Under the wrong parent | 9 | 0 |
| ✏️ Outdated name / zip | 13 | 0 |
| 👯 Extra duplicate accounts | 7 | 0 |
| ⚠️ Left Bellhaven, not flagged | 3 | 0 |
| 💵 Billing accounts (revenue AND AR) whose parent was changed | n/a | **0 of 11** |

A second run on the corrected CRM proposes **0 new fixes**: the system converges instead of churning. The app's
**🏁 Results** page recomputes this scorecard live after every pipeline run.

**28 fixes:** 8 name/zip updates · 4 direct re-parents · 2 CHOW (new account + link) · 1 sold-and-linked ·
7 duplicates retired · 4 new accounts · 2 flagged Needs Review.

## Insights: why the CRM broke

| Root cause | Fixes | What happened |
|---|---|---|
| 🏢 Acquisition leftovers | **10** | Records still under Harborview / Cedar Trail after Bellhaven bought them (wrong parent, old copies) |
| ✏️ Renamed, CRM never updated | 7 | The facility got a new name; the CRM kept the old one |
| 🆕 New community never added or linked | 5 | Joined Bellhaven, but nobody created / linked it in the CRM |
| 🚪 Left Bellhaven | 3 | Sold (Sandusky → Millstone) or gone (Alliance, Coldwater) but still listed under Bellhaven |
| ⌨️ Data-entry errors | 3 | A zip typo and facilities entered twice |

- **Acquisitions are the #1 cause.** Links break when ownership changes, not randomly.
- **Money at stake:** the 3 ownership changes with billing history (Marietta, Tiffin, Sandusky) carry **$265,250 lifetime
  revenue and $21,400 outstanding AR**. The billing rule keeps those balances collectible on the original accounts while
  sales works with the new owner.

**Recommendations**
1. Trigger this check on every acquisition or divestiture announcement, not only the daily run.
2. Make "create / link in CRM" a step in onboarding a new community (5 were missed).
3. Validate addresses on entry (zip ↔ city check, duplicate-address warning) to stop typos and double entries.

## Ambiguous cases: decision and deciding evidence

| Case | Conclusion | Deciding evidence | Action |
|---|---|---|---|
| Sunny Acres Retirement Home vs Bellhaven Willow Creek | Same facility, renamed | Identical street + zip | Rename |
| Riverbend Manor; Chesterton Senior Commons | Same facilities, renamed | Address + website manager is a CRM contact | Rename |
| Amberly Manor, Hudson OH vs Amberly Manor, Colorado | Different facilities | Different state; a shared name is not evidence | Create new |
| Bellhaven of Carlisle PA vs New Carlisle OH | Different facilities | Different street, city, state | Create new |
| Bellhaven at Union Square vs Union Square Senior Living | Different facilities | Different street and manager; score 24 | Create new |
| Ashtabula, CRM address "PO Box 517" | Same facility | Zip + phone + manager (score 75) | No change |
| Portsmouth zip 45626 vs 45662 | Same facility, typo | Same street, phone, manager | Fix zip |
| Kettering: 3 empty records, one address | One facility | Clean address + Harborview (2025) lineage | 1 moved + renamed, 2 retired |
| Lima: $47K revenue, $0 AR | Move directly | SOP needs revenue AND AR for CHOW | Re-parent |
| Marietta, Tiffin: revenue AND AR, wrong parent | CHOW | Billing SOP | New account + chow link |
| Sandusky: off website, Millstone at same address | Sold (CHOW) | Revenue AND AR; new owner's account exists | Link to it, no duplicate |
| Alliance, Coldwater: off website, no new owner | Unknown | Inactive could hide a sold facility | Needs Review + note |

## Matching approach

**Buildings don't move; names do.** A website location and a CRM account are the same facility when
the street address matches after normalising (`Avenue`→`ave`, `Northwest`→`nw`, `West`→`w`, …). Score:

| Signal | Points |
|---|---|
| Street + zip match | 60 |
| Street + city match, zip differs (catches typos like Portsmouth) | 50 |
| Same zip only, different street | 10 |
| Same city | 5 |
| Phone matches website | 20 |
| Website administrator is a CRM contact on the account | 25 |
| Name similarity | 0–15 |

Different state → never a match. Different street AND zip → never a match.

**Three confidence zones (reconsideration factor).** A plain yes/no hides how sure the system is, so scores fall in zones:

| Zone | Score | Meaning | Reviewer action |
|---|---|---|---|
| ✅ Confident | 75+ | Address + at least one more clue (phone, manager, similar name) | Approve on the evidence |
| 🔁 Reconsider | 50–74 | Only the address agrees (renamed facility, empty duplicate) | Look harder or verify with the facility first |
| 🔁 Possible match | 20–49 | New accounts only: a CRM look-alike exists | Check it's really different before creating |
| ❌ No match | < 20 | Nothing similar | Create |

75 = address (60) + city (5) + at least one more real clue. On this data the Reconsider zone flags exactly the judgment
calls: Sunny Acres (renamed, 69), the three Kettering records (72–74), the Harborview/Cedar Trail duplicate copies
(71–73), Union Square (look-alike scored 24) and the two facilities that left the website.
This handles the traps in the data:

- **Renamed facilities** (Riverbend Manor → Bellhaven of Chagrin Falls): address match, name ignored.
- **Same name, different facility** (Amberly Manor in Colorado vs Ohio; Carlisle PA vs New Carlisle OH): no address match.
- **Odd CRM address** (Ashtabula listed as "PO Box 517"): rescued by zip + phone + administrator contact (score 75).
- **Hidden location** (Findlay is linked only from the homepage, not the directory): scraper crawls both.

**Duplicates.** When several accounts match one location, one survives, chosen by:
billing history → number of contacts → already under Bellhaven → phone/administrator evidence →
cleanest address → acquisition lineage (Harborview 2025 before Cedar Trail 2026, from the About page).
Losers get `duplicate_of_account = survivor` and `Inactive`.

## Billing SOP (CHOW)

When an account must change parent:

- **revenue > 0 AND outstanding AR > 0** → leave the old account untouched, create a new account under the
  correct parent, set `chow_current_account` on the old one. If the new owner **already has** an account at
  that address (Sandusky → Millstone), link to it instead of creating a duplicate.
- **otherwise** → re-parent the existing account directly (Lima has $47K revenue but $0 AR, so it moves directly).

The rule is checked **twice**: when the proposal is made, and again at approval time against live CRM data.
The test suite verifies that CHOW'd accounts differ from the original **only** in `chow_current_account`.

## Judgment calls

- **Gone from website, no new owner found → Needs Review, not Inactive.** Leaving the website tells us something
  changed, not what. Inactive would hide a possibly-sold facility from sales.
- **Union Square Senior Living** (240 Market St) is *not* Bellhaven at Union Square (118 Union Square Dr):
  different street, different administrator, so a new account is created and the near-miss is shown as evidence.
- **Not updated:** phone numbers (CRM and website phones disagree widely; weak signal), cosmetic street
  differences ("Blvd" vs "Boulevard"), billing PO boxes, care types. Changing them would add noise for reviewers.

## Review decisions

All 28 proposals were audited against every one of the 121 accounts before approval, and all 28 are supported by
the evidence. The demo video shows the reject path on the Kettering Senior Campus duplicate (all three Kettering
copies are empty, so a reviewer could reasonably hold it for confirmation). After the demo it was sent back to the
queue and approved, so the final CRM is fully corrected.

## Safety and re-runs

- The pipeline is **read-only**. Only the Approve button writes.
- Every proposal has a **fingerprint** (type + account + target values, excluding dates/notes). Decided fingerprints
  are never re-queued; pending ones are refreshed; ones today's data no longer supports are marked *superseded*.
- Before writing, the executor **re-reads** each account and refuses if it drifted (e.g. someone already re-parented it),
  and refuses to create an account if one already exists at that address.
- Every write appends a dated note to the account explaining what changed and why (audit trail).
- Tested: approve everything → the next run produces **0 new proposals** and all 35 locations match cleanly.

## Built for a team to use and iterate

- **⚙️ Tunable rules.** All points and thresholds live in `rules.json`. The Rules page edits them, **previews the impact
  on today's data** ("28 fixes → 31, reconsider 10 → 17", plus exactly which fixes would appear or disappear) and only
  then saves. The daily run uses the saved rules, so the team iterates without code changes.
- **📝 Reviewer notes.** Every decision can carry a note. On Approve it is stored in the audit log **and written into the
  CRM account's note** next to the system's own explanation.
- **📤 Export.** CSV downloads: audit log (every fix, decision, note, what was written), field-level changes
  (before → after), verification emails and replies, the CRM and website snapshots, and the already-correct list.
- **↩️ Undo** and **✉️ facility verification**, below.

## Optional facility verification (email)

For any fix, the reviewer can open **✉️ Verify with the facility**. A generic email is drafted for that type of
fix (it includes the outstanding balance for past-due accounts). The reviewer edits it, sends it from their own
email ("Open in my email app"; the tool never sends anything itself), and records the reply as **YES** or **NO**
with the text. The reply shows on the fix as evidence and on the **✉️ Verification** page. The reviewer still
makes the final decision, so the assignment's "nothing writes without approval" rule holds.

If a past-due balance is paid later, the next daily run sees AR = $0 and automatically replaces the CHOW proposal
with a plain re-parent (tested).

## Schedule

`.github/workflows/daily-sync.yml` runs `pipeline.py` daily at 13:17 UTC with the token in a GitHub secret and commits the
updated queue. Local alternative:

```
17 6 * * * cd ~/bellhaven && python3 pipeline.py >> logs/pipeline.log 2>&1
```

## How AI was used

I used Claude as a pair programmer. It wrote a read-only exploration script first, so decisions were based on the real data
rather than assumptions. It then analysed all 121 accounts against the 35 locations and laid out the judgment calls (matching
priority, duplicate survivor, unknown-owner handling, Sandusky) with trade-offs for me to decide. I checked its work by
reviewing every proposal in the app before approving, and with an end-to-end test against a fake CRM. That test caught a real
pagination bug in the first version (only 50 of 121 accounts were loaded).

## What I'd build next

1. **Automated verification emails**: send the drafted email with signed one-click YES/NO links (hosted endpoint).
   A YES attaches the confirmation to the fix and moves it to the top of the queue; the reviewer still approves.
   Plus a daily email digest of new fixes to the sales/data team.
2. More sources of ownership truth (state licensing databases, CMS provider data) to identify the new owner when a facility disappears.
3. Fuzzy address matching / geocoding for typos beyond the normalisation table.
4. Contacts sync: website administrators vs CRM contacts, flagging departed staff.
5. Scale to many operators with per-operator scraper configs, plus a Slack digest of the daily queue.
6. Bulk approve for high-confidence, low-risk changes (e.g. renames), with sampling-based QA.

**Time spent:** _fill in_
