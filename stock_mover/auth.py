"""Sign-in and roles for the StockFlow app.

Users come from Streamlit secrets (.streamlit/secrets.toml) or the STOCKFLOW_USERS environment variable (JSON):

    [users.priya]
    password = "pbkdf2$200000$<salt>$<hash>"     # make one with:  python auth.py
    role = "approver"                            # viewer | approver | admin
    stores = ""                                  # optional: comma-separated stores this user may see ("" = all)

Roles
  viewer    sees the plan, problems and stores; cannot approve, release transfer orders or change settings
  approver  + approves trips, downloads and releases transfer orders, records receipts
  admin     + changes settings and accepts feedback-tuned settings

With no users configured the app runs in open demo mode and says so on every page: fine for the demo data,
not for a brand's real data.
"""
import getpass
import hashlib
import hmac
import json
import os
import secrets as pysecrets

ROLES = {"viewer": 0, "approver": 1, "admin": 2}


def hash_password(pw, iterations=200_000):
    salt = pysecrets.token_hex(16)
    h = hashlib.pbkdf2_hmac("sha256", pw.encode(), bytes.fromhex(salt), iterations).hex()
    return f"pbkdf2${iterations}${salt}${h}"


def check_password(pw, stored):
    try:
        _, it, salt, h = stored.split("$")
        got = hashlib.pbkdf2_hmac("sha256", pw.encode(), bytes.fromhex(salt), int(it)).hex()
        return hmac.compare_digest(got, h)
    except (ValueError, AttributeError):
        return False


def users(st_secrets=None):
    u = {}
    try:
        if st_secrets is not None and "users" in st_secrets:
            u = {k: dict(v) for k, v in st_secrets["users"].items()}
    except Exception:  # noqa: BLE001  (no secrets file)
        u = {}
    if not u and os.environ.get("STOCKFLOW_USERS"):
        u = json.loads(os.environ["STOCKFLOW_USERS"])
    return u


def can(role, need):
    return ROLES.get(role, -1) >= ROLES[need]


if __name__ == "__main__":
    print(hash_password(getpass.getpass("New password: ")))
