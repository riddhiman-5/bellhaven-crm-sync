"""Shared settings. The API token is never stored in code: put it in token.txt (git-ignored) or the BH_TOKEN env var."""
import os

BASE = "https://analyst-assessment-production.up.railway.app"
API = BASE + "/api/v1"
CRM_BROWSER = BASE + "/crm/bh_WZ_F1RSSEfJf_H9AfrjkMg/accounts"

BELLHAVEN_PARENT_NAME = "Bellhaven Senior Living (Parent Account)"

# Operators that Bellhaven acquired (from the website's About page). Used only as a
# tie-breaker when choosing which duplicate CRM record survives: earlier acquisition first.
ACQUIRED_FAMILIES = [
    "Harborview Care Group (Parent Account)",      # joined 2025
    "Cedar Trail Communities (Parent Account)",    # select communities joined 2026
]

DB_PATH = os.environ.get("BH_DB", "state.db")
DATA_DIR = "data"
USER_AGENT = "bellhaven-crm-sync/1.0"


def token():
    t = os.environ.get("BH_TOKEN")
    if not t and os.path.exists("token.txt"):
        t = open("token.txt").read().strip()
    if not t:
        raise SystemExit("No API token found. Put it in token.txt (one line) or set BH_TOKEN.")
    return t
