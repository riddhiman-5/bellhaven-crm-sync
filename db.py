"""SQLite state: every proposal ever made, keyed by a stable fingerprint, plus its decision.

This is what makes daily re-runs safe: a proposal that was approved or rejected keeps its
fingerprint, so the next run recognises it and does not put it back in the queue.
"""
import json
import sqlite3
from datetime import datetime, timezone

from config import DB_PATH

SCHEMA = """
CREATE TABLE IF NOT EXISTS proposals (
    fingerprint TEXT PRIMARY KEY,
    kind        TEXT NOT NULL,
    title       TEXT NOT NULL,
    payload     TEXT NOT NULL,
    status      TEXT NOT NULL,          -- pending | applied | rejected | superseded
    first_seen  TEXT NOT NULL,
    last_seen   TEXT NOT NULL,
    decided_at  TEXT,
    reason      TEXT,                   -- reviewer's rejection reason
    result      TEXT                    -- what was written to the CRM, or last error
);
CREATE TABLE IF NOT EXISTS verifications (
    fingerprint TEXT PRIMARY KEY,       -- the proposal this email is about
    to_addr     TEXT,
    subject     TEXT,
    body        TEXT,
    status      TEXT NOT NULL,          -- drafted | sent | confirmed | denied
    reply       TEXT,                   -- what the facility wrote back
    updated_at  TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS runs (
    run_id      INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at  TEXT NOT NULL,
    summary     TEXT NOT NULL,
    confirmed   TEXT NOT NULL
);
"""


def now():
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%SZ")


def connect():
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    con.executescript(SCHEMA)
    try:  # migrate databases created before undo existed
        con.execute("ALTER TABLE proposals ADD COLUMN undo TEXT")
    except sqlite3.OperationalError:
        pass
    return con


def migrate_fingerprints(con):
    """Re-key stored proposals with the current fingerprint rule (it once wrongly included note text,
    which contains dates). Keeps every decision; if two rows collapse into one, the decided one wins."""
    from matcher import fingerprint_of
    rank = {"applied": 3, "rejected": 2, "pending": 1, "superseded": 0}
    rows = [dict(r) for r in con.execute("SELECT * FROM proposals")]
    groups = {}
    for r in rows:
        groups.setdefault(fingerprint_of(json.loads(r["payload"])), []).append(r)
    for new_fp, rs in groups.items():
        rs.sort(key=lambda r: (rank.get(r["status"], 0), r["last_seen"]), reverse=True)
        keep = rs[0]
        for extra in rs[1:]:
            con.execute("DELETE FROM proposals WHERE fingerprint=?", (extra["fingerprint"],))
            con.execute("UPDATE OR IGNORE verifications SET fingerprint=? WHERE fingerprint=?", (keep["fingerprint"], extra["fingerprint"]))
        if keep["fingerprint"] != new_fp:
            con.execute("UPDATE proposals SET fingerprint=? WHERE fingerprint=?", (new_fp, keep["fingerprint"]))
            con.execute("UPDATE OR IGNORE verifications SET fingerprint=? WHERE fingerprint=?", (new_fp, keep["fingerprint"]))


def record_run(proposals, confirmed, meta):
    """Store this run's proposals. Returns a summary dict."""
    con = connect()
    ts = now()
    with con:
        migrate_fingerprints(con)
    seen, new, skipped_decided, refreshed = set(), 0, 0, 0
    with con:
        for p in proposals:
            fp = p["fingerprint"]
            seen.add(fp)
            row = con.execute("SELECT status FROM proposals WHERE fingerprint=?", (fp,)).fetchone()
            if row is None:
                con.execute("INSERT INTO proposals (fingerprint, kind, title, payload, status, first_seen, last_seen) "
                            "VALUES (?,?,?,?, 'pending', ?, ?)", (fp, p["kind"], p["title"], json.dumps(p), ts, ts))
                new += 1
            elif row["status"] in ("pending", "superseded"):
                # still (or again) supported by today's data: keep / put back in the queue with fresh evidence
                if row["status"] == "superseded":
                    con.execute("UPDATE proposals SET status='pending', decided_at=NULL, reason=NULL WHERE fingerprint=?", (fp,))
                # still open: refresh evidence with today's data
                con.execute("UPDATE proposals SET payload=?, title=?, last_seen=? WHERE fingerprint=?",
                            (json.dumps(p), p["title"], ts, fp))
                refreshed += 1
            else:
                # already decided (approved/rejected) - never re-queue
                con.execute("UPDATE proposals SET last_seen=? WHERE fingerprint=?", (ts, fp))
                skipped_decided += 1
        # pending proposals that today's data no longer supports are withdrawn, not left dangling
        stale = [r["fingerprint"] for r in con.execute("SELECT fingerprint FROM proposals WHERE status='pending'")
                 if r["fingerprint"] not in seen]
        for fp in stale:
            con.execute("UPDATE proposals SET status='superseded', decided_at=?, reason=? WHERE fingerprint=?",
                        (ts, "No longer proposed by the latest run (CRM or website changed)", fp))
        summary = {**meta, "proposals_this_run": len(proposals), "new": new, "still_pending": refreshed,
                   "already_decided_skipped": skipped_decided, "superseded": len(stale), "confirmed": len(confirmed)}
        con.execute("INSERT INTO runs (started_at, summary, confirmed) VALUES (?,?,?)",
                    (ts, json.dumps(summary), json.dumps(confirmed)))
    con.close()
    return summary


def list_proposals(status):
    con = connect()
    rows = con.execute("SELECT * FROM proposals WHERE status=? ORDER BY first_seen, kind, title", (status,)).fetchall()
    con.close()
    return [dict(r) | {"payload": json.loads(r["payload"])} for r in rows]


def counts():
    con = connect()
    out = {r["status"]: r["n"] for r in con.execute("SELECT status, COUNT(*) n FROM proposals GROUP BY status")}
    con.close()
    return out


def last_run():
    con = connect()
    r = con.execute("SELECT * FROM runs ORDER BY run_id DESC LIMIT 1").fetchone()
    con.close()
    if not r:
        return None
    return {"started_at": r["started_at"], "summary": json.loads(r["summary"]), "confirmed": json.loads(r["confirmed"])}


def decide(fingerprint, status, reason=None, result=None, undo=None):
    con = connect()
    with con:
        con.execute("UPDATE proposals SET status=?, decided_at=?, reason=?, result=?, undo=? WHERE fingerprint=?",
                    (status, now(), reason, result, json.dumps(undo) if undo is not None else None, fingerprint))
    con.close()


def reopen(fingerprint, note):
    """After an undo: put the proposal back in the review queue."""
    con = connect()
    with con:
        con.execute("UPDATE proposals SET status='pending', decided_at=NULL, undo=NULL, result=? WHERE fingerprint=?",
                    (note, fingerprint))
    con.close()


def set_error(fingerprint, error):
    con = connect()
    with con:
        con.execute("UPDATE proposals SET result=? WHERE fingerprint=?", ("ERROR: " + error, fingerprint))
    con.close()


# ------------------------------------------------------------------ verification emails
def get_verification(fingerprint):
    con = connect()
    r = con.execute("SELECT * FROM verifications WHERE fingerprint=?", (fingerprint,)).fetchone()
    con.close()
    return dict(r) if r else None


def save_verification(fingerprint, status, to_addr=None, subject=None, body=None, reply=None):
    con = connect()
    cur = con.execute("SELECT * FROM verifications WHERE fingerprint=?", (fingerprint,)).fetchone()
    cur = dict(cur) if cur else {}
    with con:
        con.execute("INSERT OR REPLACE INTO verifications (fingerprint, to_addr, subject, body, status, reply, updated_at) "
                    "VALUES (?,?,?,?,?,?,?)",
                    (fingerprint, to_addr if to_addr is not None else cur.get("to_addr"),
                     subject if subject is not None else cur.get("subject"),
                     body if body is not None else cur.get("body"), status,
                     reply if reply is not None else cur.get("reply"), now()))
    con.close()


def list_verifications():
    con = connect()
    rows = con.execute("SELECT v.*, p.title, p.kind, p.status AS proposal_status FROM verifications v "
                       "LEFT JOIN proposals p ON p.fingerprint = v.fingerprint ORDER BY v.updated_at DESC").fetchall()
    con.close()
    return [dict(r) for r in rows]


def all_proposals():
    con = connect()
    rows = con.execute("SELECT * FROM proposals ORDER BY first_seen, kind, title").fetchall()
    con.close()
    return [dict(r) | {"payload": json.loads(r["payload"])} for r in rows]
