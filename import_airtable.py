"""
import_airtable.py — copy every task from Airtable into the Task Database sheet.

Run it from GitHub: Actions → Import tasks from Airtable → Run workflow, typing
"yes" in the confirm box. Safe to run more than once BEFORE you switch over:
each run replaces every row that came from Airtable (IDs starting "rec") with
Airtable's current version, and keeps any row added in the sheet since.

Do NOT run it after the switch-over: from then on the sheet is the master, and
a run would overwrite edits made in the sheet with Airtable's stale copy.

Needs the AIRTABLE_API_KEY and GOOGLE_SA_JSON secrets.
"""

import os
import json
import requests
from googleapiclient.discovery import build
from google.oauth2 import service_account

AIRTABLE_BASE = "app8HGKiLT6cY5zZi"          # Task Management HQ
AT_TASKS      = "tblIGUa96EX3BtcP7"
SHEET_ID      = "1IduH-Kr8TWdOyhDJXxfOrVT6og4q1F1KyGVUO0mHsQ8"
TAB           = "Tasks"
MESSAGE_CAP   = 20000                         # a sheet cell holds 50,000 characters

# Sheet column → Airtable field. Order = the sheet's columns A..U.
COLUMNS = [
    ("ID",            None),
    ("Task",          "Task"),
    ("Status",        "Status"),
    ("Horizon",       "Horizon"),
    ("Priority",      "Priority"),
    ("Due Date",      "Due Date"),
    ("Client",        "Client"),
    ("Work Type",     "Work Type"),
    ("Deadline Type", "Deadline Type"),
    ("Action By",     "Action By"),
    ("Source",        "Source"),
    ("Sender",        "Sender"),
    ("Received",      "Received"),
    ("Notes",         "Notes"),
    ("Message",       "Message"),
    ("Email Link",    "Original Email Link"),
    ("Meeting",       "Meeting"),
    ("Related Work",  "Related Work"),
    ("Created",       None),
    ("Updated",       None),
    ("Message ID",    "Message ID"),
]
HEADER = [c for c, _ in COLUMNS]

# Airtable still carries plain duplicates of three Horizon options
HORIZON_FIX = {"Today": "🔴 Today", "This Week": "🟡 This Week", "Next Week": "🔵 Next Week"}


def airtable_records():
    headers = {"Authorization": f"Bearer {os.environ['AIRTABLE_API_KEY']}"}
    url     = f"https://api.airtable.com/v0/{AIRTABLE_BASE}/{AT_TASKS}"
    params  = {"pageSize": 100}
    out     = []
    while True:
        r = requests.get(url, headers=headers, params=params, timeout=30)
        r.raise_for_status()
        data = r.json()
        out.extend(data.get("records", []))
        if not data.get("offset"):
            return out
        params["offset"] = data["offset"]


def to_row(rec):
    """One Airtable record (REST shape: {'id', 'createdTime', 'fields'}) as a sheet row."""
    f   = rec.get("fields", {})
    row = []
    for col, field in COLUMNS:
        if col == "ID":
            v = rec["id"]
        elif col == "Created":
            v = rec.get("createdTime", "")
        elif col == "Updated":
            v = ""
        else:
            v = f.get(field, "")
        if isinstance(v, list):                 # multi-selects, linked records
            v = ", ".join(str(x) for x in v)
        v = "" if v is None else str(v).strip()
        if col == "Horizon":
            v = HORIZON_FIX.get(v, v)
        if col == "Due Date":
            v = v[:10]
        if col == "Message" and len(v) > MESSAGE_CAP:
            v = v[:MESSAGE_CAP] + " … [cut at import; full text in the Airtable export]"
        row.append(v)
    return row


def build_rows(records, existing):
    """Airtable's rows first, then any row the sheet gained that didn't come from Airtable.

    `existing` is the sheet's current data rows (lists, header excluded).
    """
    rows = [to_row(r) for r in sorted(records, key=lambda r: r.get("createdTime", ""))]
    kept = [
        (r + [""] * len(HEADER))[:len(HEADER)]
        for r in existing
        if r and any(str(c).strip() for c in r) and not str(r[0]).startswith("rec")
    ]
    return rows, kept


def main():
    if os.environ.get("CONFIRM", "").strip().lower() != "yes":
        raise SystemExit("Not confirmed: type yes in the workflow's confirm box to run the import.")

    creds = service_account.Credentials.from_service_account_info(
        json.loads(os.environ["GOOGLE_SA_JSON"]),
        scopes=["https://www.googleapis.com/auth/spreadsheets"])
    values = build("sheets", "v4", credentials=creds, cache_discovery=False).spreadsheets().values()

    current = values.get(spreadsheetId=SHEET_ID, range=f"'{TAB}'").execute().get("values", [])
    if current and [str(h).strip() for h in current[0]][:len(HEADER)] != HEADER:
        raise SystemExit(f"The Tasks tab's header row isn't the expected one — stopping.\n{current[0]}")

    records = airtable_records()
    rows, kept = build_rows(records, current[1:])
    print(f"Airtable: {len(records)} tasks · kept from the sheet: {len(kept)} rows added there")

    values.clear(spreadsheetId=SHEET_ID, range=f"'{TAB}'!A2:U").execute()
    values.update(
        spreadsheetId=SHEET_ID, range=f"'{TAB}'!A1",
        valueInputOption="RAW",
        body={"values": [HEADER] + rows + kept},
    ).execute()

    open_count = sum(1 for r in rows if r[HEADER.index("Status")] != "Done")
    print(f"Wrote {len(rows) + len(kept)} rows ({open_count} open from Airtable).")


if __name__ == "__main__":
    main()
