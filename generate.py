"""
generate.py — Daily Dashboard
Pulls from: Notion (tasks + emails), Google Calendar (work + family), Claude AI
Writes index.html to GitHub Pages
"""

import os
import re
import json
import requests
from datetime import date, datetime, timedelta, timezone
import anthropic
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from googleapiclient.discovery import build

# ── CONFIG ────────────────────────────────────────────────────────────────────

NOTION_TASKS_DB    = "d5669af6-6732-432b-b1ac-558a860164ca"
NOTION_EMAILS_DB   = "a34ff292-5e7c-4069-ba90-91de918e380f"
NOTION_CLIENTS_DB  = "81b6953304db4b76ba7a25a9704fe7b2"
NOTION_VERSION     = "2022-06-28"
WORKER_URL         = "https://notion-proxy.jgilbert82.workers.dev"

WORK_CAL_ID        = "86iqekmmn19f3b7j1r9ihepkt9ethdtc@import.calendar.google.com"
FAMILY_CAL_ID      = "family11040178401192019419@group.calendar.google.com"
TANIA_CAL_ID       = "tania.andreasen80@gmail.com"
PERSONAL_CAL_ID    = "jgilbert82@gmail.com"

CLIENT_COLOURS = {
    "AEW": "#2563eb", "SSCP": "#16a34a", "Ingka": "#d97706",
    "Hedeland": "#d97706", "Mileway": "#7c3aed", "AXA": "#0891b2",
    "Arrow": "#dc2626", "EQT": "#be185d", "M&G": "#065f46",
    "BNPP": "#1d4ed8", "CBRE Internal": "#64748b",
}

COPENHAGEN = timezone(timedelta(hours=2))  # CEST summer


# ── HELPERS ───────────────────────────────────────────────────────────────────

def notion_headers():
    return {
        "Authorization": f"Bearer {os.environ['NOTION_API_KEY']}",
        "Content-Type": "application/json",
        "Notion-Version": NOTION_VERSION,
    }

def esc(s):
    return (s or "").replace("&","&amp;").replace("<","&lt;").replace(">","&gt;").replace('"',"&quot;")

def fmt_date(d):
    try:
        return datetime.strptime(d, "%Y-%m-%d").strftime("%-d %b")
    except Exception:
        return d or ""

def fmt_time(dt_str):
    """Parse ISO datetime string and return HH:MM in Copenhagen time."""
    try:
        d = datetime.fromisoformat(dt_str)
        if d.tzinfo is not None:
            d = d.astimezone(COPENHAGEN)
        return d.strftime("%H:%M")
    except Exception:
        return dt_str[:5] if dt_str else ""

def client_colour(name):
    for key, col in CLIENT_COLOURS.items():
        if key.lower() in (name or "").lower():
            return col
    return "#64748b"

def clean_title(title):
    title = re.sub(r'^Day Book[^:]+:\s*', '', title)
    title = re.sub(r'^Inbox\s*:\s*', '', title)
    return title.strip()

def extract_teams_link(description):
    """Extract a Teams join link from a calendar event description."""
    if not description:
        return None
    m = re.search(r'https://teams\.microsoft\.com/l/meetup-join/[^\s<>"]+', description)
    if m:
        return m.group(0)
    m = re.search(r'https://teams\.microsoft\.com/meet/[^\s<>"]+', description)
    return m.group(0) if m else None


# ── GOOGLE CALENDAR ───────────────────────────────────────────────────────────

def get_calendar_service():
    creds_data = json.loads(os.environ["GOOGLE_CREDENTIALS_JSON"])
    creds = Credentials(
        token=creds_data["token"],
        refresh_token=creds_data["refresh_token"],
        token_uri="https://oauth2.googleapis.com/token",
        client_id=creds_data["client_id"],
        client_secret=creds_data["client_secret"],
        scopes=["https://www.googleapis.com/auth/calendar.readonly"],
    )
    if creds.expired or not creds.valid:
        creds.refresh(Request())
    return build("calendar", "v3", credentials=creds)


def fetch_cal_events(service, cal_id, date_str):
    """Fetch all events for a calendar on a given date (YYYY-MM-DD)."""
    try:
        result = service.events().list(
            calendarId=cal_id,
            timeMin=date_str + "T00:00:00+02:00",
            timeMax=date_str + "T23:59:59+02:00",
            singleEvents=True,
            orderBy="startTime",
            maxResults=20,
        ).execute()
        events = []
        for item in result.get("items", []):
            start = item.get("start", {})
            end   = item.get("end", {})
            if "dateTime" not in start:
                # All-day event
                events.append({
                    "summary":     item.get("summary", "Untitled"),
                    "start":       "All day",
                    "end":         "",
                    "location":    (item.get("location") or "")[:60],
                    "teams_link":  None,
                    "all_day":     True,
                    "date":        date_str,
                })
                continue
            # Check not declined
            attendees = item.get("attendees", [])
            if any(a.get("self") and a.get("responseStatus") == "declined" for a in attendees):
                continue
            events.append({
                "summary":    item.get("summary", "Untitled"),
                "start":      fmt_time(start["dateTime"]),
                "end":        fmt_time(end["dateTime"]),
                "location":   (item.get("location") or "")[:60],
                "teams_link": extract_teams_link(item.get("description")),
                "all_day":    False,
                "date":       date_str,
            })
        return events
    except Exception as e:
        print(f"    WARNING: cal fetch failed for {cal_id} on {date_str}: {e}")
        return []


def fetch_all_calendar_data(today_str):
    """Fetch work calendar for today + 2 days, and family calendars for 7 days."""
    try:
        service = get_calendar_service()
        today   = date.fromisoformat(today_str)

        # Work calendar: today + next 2 days
        work_days = {}
        for i in range(3):
            d = (today + timedelta(days=i)).isoformat()
            events = fetch_cal_events(service, WORK_CAL_ID, d)
            if events:
                work_days[d] = events

        # Family + Tania: next 7 days
        family_events = []
        for i in range(7):
            d = (today + timedelta(days=i)).isoformat()
            fam  = fetch_cal_events(service, FAMILY_CAL_ID, d)
            tan  = fetch_cal_events(service, TANIA_CAL_ID, d)
            for e in fam:
                e["source"] = "family"
            for e in tan:
                e["source"] = "tania"
            family_events.extend(fam + tan)

        total_work = sum(len(v) for v in work_days.values())
        print(f"  Work calendar: {total_work} events over 3 days")
        print(f"  Family calendar: {len(family_events)} events over 7 days")
        return work_days, family_events

    except Exception as e:
        print(f"  WARNING: Calendar fetch failed: {e}")
        return {}, []


# ── NOTION CLIENT MAP ─────────────────────────────────────────────────────────

def build_client_map():
    client_map = {}
    cursor = None
    while True:
        body = {"page_size": 100}
        if cursor:
            body["start_cursor"] = cursor
        resp = requests.post(
            f"https://api.notion.com/v1/databases/{NOTION_CLIENTS_DB}/query",
            headers=notion_headers(), json=body,
        )
        resp.raise_for_status()
        data = resp.json()
        for page in data["results"]:
            pid   = page["id"]
            props = page["properties"]
            parts = (
                props.get("Client", {}).get("title", [])
                or props.get("Name", {}).get("title", [])
            )
            name = "".join(p.get("plain_text", "") for p in parts).strip()
            if name:
                client_map[pid] = name
                client_map[pid.replace("-", "")] = name
    
        if not data.get("has_more"):
            break
        cursor = data["next_cursor"]
    print(f"  Client map: {len(client_map)//2} clients")
    return client_map


# ── NOTION TASKS ──────────────────────────────────────────────────────────────

def parse_client(prop, client_map):
    rels = prop.get("relation", [])
    if not rels:
        return None
    pid = rels[0].get("id", "")
    return client_map.get(pid) or client_map.get(pid.replace("-", ""))

def is_done(props):
    if (props.get("Status", {}).get("select") or {}).get("name") == "Done":
        return True
    return bool(props.get("Done", {}).get("checkbox", False))

def fetch_tasks(client_map):
    all_tasks = []
    seen = {}
    cursor = None
    while True:
        body = {"page_size": 100}
        if cursor:
            body["start_cursor"] = cursor
        resp = requests.post(
            f"https://api.notion.com/v1/databases/{NOTION_TASKS_DB}/query",
            headers=notion_headers(), json=body,
        )
        resp.raise_for_status()
        data = resp.json()
        for page in data["results"]:
            props = page["properties"]
            if is_done(props):
                continue
            parts = props.get("Task", {}).get("title", [])
            title = "".join(p.get("plain_text", "") for p in parts).strip()
            if not title:
                continue
            seen[title] = seen.get(title, 0) + 1
            if seen[title] > 1:
                continue
            status    = (props.get("Status", {}).get("select") or {}).get("name", "") or "Not Started"
            horizon   = (props.get("Horizon", {}).get("select") or {}).get("name", "") or ""
            priority  = (props.get("Priority", {}).get("select") or {}).get("name", "") or ""
            work_type = (props.get("Work Type", {}).get("select") or {}).get("name", "") or ""
            due_obj   = props.get("Due Date", {}).get("date") or {}
            due       = (due_obj.get("start") or "")[:10] or None
            client    = parse_client(props.get("Client", {}), client_map)
            msg       = "".join(p.get("plain_text","") for p in props.get("Message",{}).get("rich_text",[])).strip()
            notes     = "".join(p.get("plain_text","") for p in props.get("Notes",{}).get("rich_text",[])).strip()
            context   = (msg or notes or "")[:250] or None
            all_tasks.append({
                "title": title, "status": status, "horizon": horizon,
                "due": due, "priority": priority, "work_type": work_type,
                "client": client, "context": context,
                "id": page["id"], "url": page["url"],
            })
        if not data.get("has_more"):
            break
        cursor = data["next_cursor"]
    print(f"  {len(all_tasks)} active tasks fetched")
    return all_tasks

def bucket_tasks(tasks, today_str):
    overdue, today_tasks, waiting, this_week, later = [], [], [], [], []
    for t in tasks:
        if t["status"] == "Waiting":
            waiting.append(t); continue
        due, horizon = t["due"], t["horizon"]
        if due and due < today_str:
            overdue.append(t)
        elif horizon == "🔴 Today" or due == today_str:
            today_tasks.append(t)
        elif horizon == "🟡 This Week":
            this_week.append(t)
        else:
            later.append(t)

    def sk(t):
        p = {"High": 0, "Medium": 1, "Low": 2}.get(t["priority"], 3)
        return (p, t["due"] or "9999")

    for b in [overdue, today_tasks, waiting, this_week, later]:
        b.sort(key=sk)
    return overdue, today_tasks, waiting, this_week, later


# ── NOTION EMAILS ─────────────────────────────────────────────────────────────

def fetch_emails(days_back=5):
    """Fetch recent emails from Notion Meeting Notes DB (via TaskRobin)."""
    cutoff = (date.today() - timedelta(days=days_back)).isoformat()
    emails = []
    cursor = None
    while True:
        body = {
            "page_size": 50,
            "filter": {
                "and": [
                    {"property": "Sender", "email": {"is_not_empty": True}},
                    {"property": "Received On", "date": {"on_or_after": cutoff}},
                ]
            },
            "sorts": [{"property": "Received On", "direction": "descending"}],
        }
        if cursor:
            body["start_cursor"] = cursor
        resp = requests.post(
            f"https://api.notion.com/v1/databases/{NOTION_EMAILS_DB}/query",
            headers=notion_headers(), json=body,
        )
        resp.raise_for_status()
        data = resp.json()
        for page in data["results"]:
            props = page["properties"]
            sender  = props.get("Sender", {}).get("email", "") or ""
            subject_parts = props.get("Subject", {}).get("rich_text", [])
            subject = "".join(p.get("plain_text","") for p in subject_parts).strip()
            msg_parts = props.get("Message", {}).get("rich_text", [])
            message = "".join(p.get("plain_text","") for p in msg_parts).strip()[:300]
            received = (props.get("Received On", {}).get("date") or {}).get("start", "")[:10]
            tags = [t.get("name","") for t in props.get("Email Tags", {}).get("multi_select", [])]
            client_legacy = (props.get("Client (legacy)", {}).get("select") or {}).get("name", "")
            link = props.get("Original Email Link", {}).get("url", "") or page["url"]
            if not sender or not subject:
                continue
            # Clean sender name from email
            sender_name = sender.split("@")[0].replace(".", " ").title() if "@" in sender else sender
            emails.append({
                "sender":   sender_name,
                "sender_email": sender,
                "subject":  subject,
                "message":  message,
                "received": received,
                "tags":     tags,
                "client":   client_legacy,
                "url":      link,
                "is_new":   "New" in tags,
                "id":       page["id"],
            })
        if not data.get("has_more") or len(emails) >= 30:
            break
        cursor = data["next_cursor"]
    print(f"  {len(emails)} emails fetched (last {days_back} days)")
    return emails


# ── AI SUMMARY ────────────────────────────────────────────────────────────────

def generate_summary(overdue, today_tasks, waiting, this_week, work_days, emails, today_str):
    client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
    today_display = date.today().strftime("%A %-d %B %Y")

    def fmt(tasks, limit=8):
        return "\n".join(
            f"  [{t.get('client') or 'No client'}] {t['title']}"
            + (f" (due {t['due']})" if t["due"] else "")
            for t in tasks[:limit]
        )

    sections = []
    # Today's meetings
    today_events = work_days.get(today_str, [])
    if today_events:
        lines = "\n".join(f"  {e['start']} {e['summary']}" for e in today_events)
        sections.append(f"TODAY'S MEETINGS:\n{lines}")
    # Tasks
    if overdue:
        sections.append(f"OVERDUE ({len(overdue)}):\n{fmt(overdue, 6)}")
    if today_tasks:
        sections.append(f"TODAY'S TASKS ({len(today_tasks)}):\n{fmt(today_tasks)}")
    if waiting:
        sections.append(f"WAITING ({len(waiting)}):\n{fmt(waiting, 5)}")
    # Recent emails needing action
    new_emails = [e for e in emails if e["is_new"]][:5]
    if new_emails:
        lines = "\n".join(f"  From {e['sender']}: {e['subject']}" for e in new_emails)
        sections.append(f"EMAILS NEEDING ACTION:\n{lines}")

    task_text = "\n\n".join(sections) or "No active tasks."

    msg = client.messages.create(
        model="claude-opus-4-5",
        max_tokens=450,
        system=(
            f"You are a sharp executive assistant briefing Joseph, Senior Property Manager "
            f"at CBRE Copenhagen. Clients: AEW (Sydmarken & Kystvejen), SSCP, Ingka/Hedeland, "
            f"Mileway, AXA Nordic, Arrow Capital, EQT, M&G. Today is {today_display}.\n\n"
            f"Write a tight morning briefing with EXACTLY these 3 section headings on their own line:\n"
            f"MUST DO TODAY\n"
            f"WATCH / CHASING\n"
            f"THIS WEEK\n\n"
            f"Be SPECIFIC — name actual tasks, meetings, and emails. Reference today's meetings where tasks link. "
            f"2-3 sentences per section. No bullets. Plain prose."
        ),
        messages=[{"role": "user", "content": task_text}],
    )
    return msg.content[0].text.strip()


# ── RENDER HELPERS ────────────────────────────────────────────────────────────

def format_summary_html(text):
    headings = {"MUST DO TODAY", "WATCH / CHASING", "THIS WEEK"}
    parts = []
    for line in text.split("\n"):
        line = re.sub(r'\*\*([^*]+)\*\*', r'\1', line.strip())
        if not line:
            continue
        if line.rstrip(":").upper() in headings or line.upper() in headings:
            parts.append(f'<div class="sum-head">{esc(line.rstrip(":"))}</div>')
        else:
            parts.append(f'<p>{esc(line)}</p>')
    return "\n".join(parts)

def client_badge_html(client, small=False):
    if not client:
        return ""
    col = client_colour(client)
    cls = "client-tag-sm" if small else "client-tag"
    return f'<span class="{cls}" style="background:{col}18;color:{col};border-color:{col}44">{esc(client)}</span>'

def pri_dot_html(priority):
    if priority == "High":
        return '<span class="dot high">●</span>'
    if priority == "Low":
        return '<span class="dot low">●</span>'
    return ""

def due_badge_html(due, today_str):
    if not due:
        return ""
    if due < today_str:
        return f'<span class="badge overdue">{fmt_date(due)}</span>'
    if due == today_str:
        return '<span class="badge today">Today</span>'
    return f'<span class="badge upcoming">{fmt_date(due)}</span>'

def render_task_card(t, today_str, compact=False):
    title    = clean_title(t["title"])
    page_id  = t["id"]
    open_lnk = f'<a class="open-link" href="{esc(t["url"])}" target="_blank">↗</a>'
    done_btn = f'<button class="done-btn" onclick="markDone(this)" data-page-id="{page_id}">✓ Done</button>'
    add_btn  = f'<button class="add-day-btn" onclick="addToMyDay(this)" data-title="{esc(title)}" data-client="{esc(t["client"] or "")}">+ Day</button>'
    due_b    = due_badge_html(t["due"], today_str)
    pri_d    = pri_dot_html(t["priority"])
    cli_b    = client_badge_html(t["client"])
    wt       = t.get("work_type","")
    wt_b     = f'<span class="wt-tag">{esc(wt)}</span>' if wt else ""
    ctx      = t.get("context","")
    ctx_html = ""
    if ctx and not compact:
        ctx_html = f'<div class="task-ctx">{esc(ctx[:180] + ("…" if len(ctx)>180 else ""))}</div>'

    if compact:
        done_sm = f'<button class="done-btn-sm" onclick="markDone(this)" data-page-id="{page_id}">✓</button>'
        add_sm  = f'<button class="add-day-btn-sm" onclick="addToMyDay(this)" data-title="{esc(title)}" data-client="{esc(t["client"] or "")}">+</button>'
        return (
            f'<div class="task-row" data-page-id="{page_id}">'
            f'<div class="task-row-left">{pri_d}<span class="task-row-title">{esc(title)}</span></div>'
            f'<div class="task-row-right">{due_b}{cli_b}{done_sm}{add_sm}{open_lnk}</div>'
            f'</div>'
        )
    return (
        f'<div class="task-card" data-page-id="{page_id}">'
        f'<div class="card-header"><div class="card-title">{pri_d} {esc(title)}</div>'
        f'<div class="card-actions">{done_btn}{add_btn}{open_lnk}</div></div>'
        f'<div class="card-meta">{due_b}{cli_b}{wt_b}</div>'
        f'{ctx_html}'
        f'</div>'
    )

def render_task_section(heading, icon, tasks, today_str, compact=False, colour="var(--accent)"):
    if not tasks:
        return ""
    items = "\n".join(render_task_card(t, today_str, compact=compact) for t in tasks)
    grid  = "task-list" if compact else "task-grid"
    return (
        f'<section class="dash-section">'
        f'<div class="sec-head" style="--sec-col:{colour}">'
        f'<span>{icon} {heading}</span><span class="sec-count">{len(tasks)}</span>'
        f'</div>'
        f'<div class="{grid}">{items}</div>'
        f'</section>'
    )


# ── WORK CALENDAR RENDER ──────────────────────────────────────────────────────

def render_work_day_col(date_str, events, today_str):
    try:
        d = date.fromisoformat(date_str)
        if date_str == today_str:
            label = "Today"
            sub   = d.strftime("%a %-d %b")
            hdr_class = "day-col-hdr today-hdr"
        elif date_str == (date.fromisoformat(today_str) + timedelta(days=1)).isoformat():
            label = "Tomorrow"
            sub   = d.strftime("%a %-d %b")
            hdr_class = "day-col-hdr"
        else:
            label = d.strftime("%A")
            sub   = d.strftime("%-d %b")
            hdr_class = "day-col-hdr"
    except Exception:
        label = date_str
        sub = ""
        hdr_class = "day-col-hdr"

    if not events:
        body = '<div class="no-meetings">No meetings</div>'
    else:
        cards = ""
        now_time = datetime.now(COPENHAGEN).strftime("%H:%M") if date_str == today_str else "00:00"
        for e in events:
            is_past  = date_str == today_str and e["start"] < now_time and not e["all_day"]
            past_cls = " past" if is_past else ""
            teams    = f'<a class="teams-link" href="{esc(e["teams_link"])}" target="_blank">Join</a>' if e["teams_link"] else ""
            loc      = f'<div class="ev-loc">📍 {esc(e["location"][:40])}</div>' if e["location"] and "Microsoft Teams" not in e["location"] else ""
            time_str = "All day" if e["all_day"] else f'{e["start"]}–{e["end"]}'
            cards += (
                f'<div class="cal-event{past_cls}">'
                f'<div class="ev-header"><span class="ev-time">{esc(time_str)}</span>{teams}</div>'
                f'<div class="ev-title">{esc(e["summary"])}</div>'
                f'{loc}'
                f'</div>'
            )
        body = cards

    return f'<div class="day-col"><div class="{hdr_class}"><strong>{label}</strong><span>{sub}</span></div>{body}</div>'


# ── EMAIL TRIAGE RENDER ───────────────────────────────────────────────────────

def render_email_triage(emails):
    if not emails:
        return '<div class="no-emails">No recent emails in the last 5 days.</div>'

    # Split: New (needs action) vs Recent (FYI)
    new_emails    = [e for e in emails if e["is_new"]]
    recent_emails = [e for e in emails if not e["is_new"]][:10]

    def email_row(e):
        col     = client_colour(e["client"])
        cli_b   = client_badge_html(e["client"], small=True) if e["client"] else ""
        new_dot = '<span class="new-dot">NEW</span>' if e["is_new"] else ""
        recv    = fmt_date(e["received"]) if e["received"] else ""
        preview = e["message"][:120] + "…" if len(e["message"]) > 120 else e["message"]
        return (
            f'<div class="email-row">'
            f'<div class="email-main">'
            f'<div class="email-header">'
            f'<span class="email-sender">{esc(e["sender"])}</span>'
            f'{new_dot}{cli_b}'
            f'<span class="email-date">{recv}</span>'
            f'</div>'
            f'<div class="email-subject"><a href="{esc(e["url"])}" target="_blank">{esc(e["subject"])}</a></div>'
            f'{"<div class=email-preview>" + esc(preview) + "</div>" if preview else ""}'
            f'</div>'
            f'</div>'
        )

    html = ""
    if new_emails:
        html += '<div class="email-group-label">⚡ Needs Action</div>'
        html += "".join(email_row(e) for e in new_emails)
    if recent_emails:
        html += '<div class="email-group-label">📬 Recent</div>'
        html += "".join(email_row(e) for e in recent_emails)
    return html


# ── FAMILY PANEL RENDER ───────────────────────────────────────────────────────

def render_family_panel(family_events, today_str):
    if not family_events:
        return '<div class="no-family">Nothing in the family calendar this week.</div>'

    # Group by date
    by_date = {}
    for e in family_events:
        d = e["date"]
        by_date.setdefault(d, []).append(e)

    rows = ""
    today = date.fromisoformat(today_str)
    for i in range(7):
        d = (today + timedelta(days=i)).isoformat()
        if d not in by_date:
            continue
        evs = by_date[d]
        try:
            d_obj = date.fromisoformat(d)
            label = "Today" if d == today_str else d_obj.strftime("%a %-d %b")
        except Exception:
            label = d
        pills = ""
        for e in evs:
            src_icon = "👨‍👩‍👧" if e["source"] == "family" else "🧑"
            time_str = "" if e["all_day"] else f' {e["start"]}'
            pills += f'<span class="fam-pill">{src_icon}{time_str} {esc(e["summary"])}</span>'
        rows += f'<div class="fam-row"><span class="fam-date">{label}</span><div class="fam-events">{pills}</div></div>'

    return rows


# ── CLIENT SUMMARY ────────────────────────────────────────────────────────────

def render_client_summary(tasks, today_str):
    from collections import defaultdict
    data = defaultdict(lambda: {"total":0,"overdue":0,"today":0,"waiting":0,"high":0})
    for t in tasks:
        c = t.get("client") or "No client"
        data[c]["total"] += 1
        if t["status"] == "Waiting":
            data[c]["waiting"] += 1
        due = t.get("due") or ""
        if due and due < today_str:
            data[c]["overdue"] += 1
        elif due == today_str:
            data[c]["today"] += 1
        if t.get("priority") == "High":
            data[c]["high"] += 1

    rows = sorted(data.items(), key=lambda x: -x[1]["total"])

    def pill(label, val, col):
        if not val:
            return ""
        return f'<span class="cs-pill" style="background:{col}18;color:{col};border-color:{col}44">{label} {val}</span>'

    html = ""
    for name, d in rows:
        col  = client_colour(name)
        pills = ""
        if d["overdue"]: pills += pill("Overdue", d["overdue"], "#c8502a")
        if d["today"]:   pills += pill("Today",   d["today"],   "#b08a20")
        if d["waiting"]: pills += pill("Waiting", d["waiting"], "#7a7468")
        html += (
            f'<div class="cs-row">'
            f'<div class="cs-name" style="color:{col}">{esc(name)}</div>'
            f'<div class="cs-pills">{pills}</div>'
            f'<div class="cs-total" style="color:{col}">{d["total"]}</div>'
            f'</div>'
        )
    count = len([r for r in rows if r[0] != "No client"])
    return f'''<div class="client-summary">
  <button class="cs-toggle" onclick="toggleCS()" id="cs-btn">
    <span>▸ Clients ({count})</span><span class="cs-hint">click to expand</span>
  </button>
  <div class="cs-body" id="cs-body" style="display:none">
    <div class="cs-header"><span>Client</span><span></span><span>Open</span></div>
    {html}
  </div>
</div>'''


# ── HTML BUILD ────────────────────────────────────────────────────────────────

def build_html(overdue, today_tasks, waiting, this_week, later, summary,
               work_days, family_events, emails, today_str):
    today_display  = datetime.strptime(today_str, "%Y-%m-%d").strftime("%A %-d %B %Y")
    generated_time = datetime.utcnow().strftime("%H:%M UTC")
    all_tasks      = overdue + today_tasks + waiting + this_week + later
    total          = len(all_tasks)

    # Sections
    summary_html    = format_summary_html(summary)
    client_sum_html = render_client_summary(all_tasks, today_str)

    # Work calendar cols (today + next 2 days)
    today    = date.fromisoformat(today_str)
    day_cols = ""
    for i in range(3):
        d      = (today + timedelta(days=i)).isoformat()
        events = work_days.get(d, [])
        day_cols += render_work_day_col(d, events, today_str)

    email_html  = render_email_triage(emails)
    family_html = render_family_panel(family_events, today_str)

    task_today_html    = render_task_section("Today",      "⚡", today_tasks, today_str, compact=False, colour="#b08a20")
    task_overdue_html  = render_task_section("Overdue",    "🔴", overdue,     today_str, compact=False, colour="#c8502a")
    task_waiting_html  = render_task_section("Waiting On", "⏳", waiting,     today_str, compact=True,  colour="#7a7468")
    task_week_html     = render_task_section("This Week",  "📅", this_week,   today_str, compact=True,  colour="#2563eb")
    task_later_html    = render_task_section("Later",      "📂", later,       today_str, compact=True,  colour="#94a3b8")

    new_email_count = len([e for e in emails if e["is_new"]])

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Daily Dashboard · {today_display}</title>
<link href="https://fonts.googleapis.com/css2?family=Playfair+Display:ital,wght@0,400;0,700;1,400&family=DM+Sans:opsz,wght@9..40,300;9..40,400;9..40,500&display=swap" rel="stylesheet">
<style>
:root{{
  --ink:#1a1a2e;--paper:#f5f0e8;--cream:#ede8dc;
  --accent:#c8502a;--gold:#b08a20;--muted:#7a7468;
  --border:#d4cfc5;--card:#faf7f2;--blue:#2563eb;
  --green:#16a34a;--sidebar:340px;
}}
*{{box-sizing:border-box;margin:0;padding:0;}}
body{{font-family:'DM Sans',sans-serif;background:var(--paper);color:var(--ink);min-height:100vh;}}

/* ── MASTHEAD ── */
.masthead{{background:var(--ink);color:var(--paper);padding:20px 40px;display:flex;align-items:center;justify-content:space-between;flex-wrap:wrap;gap:10px;border-bottom:4px solid var(--accent);}}
.mast-left h1{{font-family:'Playfair Display',serif;font-size:2rem;letter-spacing:-0.5px;line-height:1;}}
.edition{{font-size:.6rem;letter-spacing:3px;text-transform:uppercase;color:var(--accent);margin-top:5px;font-weight:500;}}
.mast-right{{text-align:right;}}
.date-line{{font-family:'Playfair Display',serif;font-size:.95rem;opacity:.85;}}
.sub{{font-size:.62rem;letter-spacing:2px;text-transform:uppercase;opacity:.4;margin-top:3px;}}
.gen-time{{font-size:.55rem;letter-spacing:1px;text-transform:uppercase;color:rgba(255,255,255,.2);margin-top:2px;}}
.refresh-btn{{font-size:.55rem;letter-spacing:1.5px;text-transform:uppercase;font-weight:600;color:rgba(255,255,255,.4);border:1px solid rgba(255,255,255,.15);background:transparent;padding:3px 10px;cursor:pointer;margin-top:5px;transition:all .2s;display:inline-block;}}
.refresh-btn:hover{{color:var(--paper);border-color:rgba(255,255,255,.5);}}
.refresh-btn.spinning{{color:rgba(255,255,255,.2);border-color:rgba(255,255,255,.08);cursor:default;}}
.refresh-btn.done{{color:#4ade80;border-color:#4ade8055;}}
.refresh-btn.error{{color:#e07a5a;border-color:#e07a5a55;}}

/* ── STATS ── */
.stats-bar{{background:var(--ink);display:flex;padding:12px 40px;border-top:1px solid rgba(255,255,255,.08);flex-wrap:wrap;gap:24px;}}
.stat strong{{font-family:'Playfair Display',serif;font-size:1.5rem;display:block;line-height:1;color:var(--paper);}}
.stat span{{font-size:.55rem;letter-spacing:2.5px;text-transform:uppercase;color:rgba(255,255,255,.3);}}
.stat.red strong{{color:#e07a5a;}}.stat.gold strong{{color:#d4aa50;}}.stat.blue strong{{color:#93c5fd;}}.stat.green strong{{color:#86efac;}}

/* ── CLIENT SUMMARY ── */
.client-summary{{background:var(--cream);border-bottom:1px solid var(--border);}}
.cs-toggle{{width:100%;background:none;border:none;padding:9px 40px;display:flex;align-items:center;justify-content:space-between;cursor:pointer;font-size:.58rem;letter-spacing:2.5px;text-transform:uppercase;font-weight:600;color:var(--ink);transition:background .15s;text-align:left;}}
.cs-toggle:hover{{background:rgba(0,0,0,.03);}}
.cs-hint{{font-size:.55rem;letter-spacing:1px;color:var(--muted);font-weight:400;text-transform:none;}}
.cs-body{{padding:8px 40px 12px;}}
.cs-header{{display:grid;grid-template-columns:160px 1fr 36px;font-size:.52rem;letter-spacing:2px;text-transform:uppercase;color:var(--muted);padding:4px 0;border-bottom:1px solid var(--border);margin-bottom:3px;}}
.cs-row{{display:grid;grid-template-columns:160px 1fr 36px;align-items:center;padding:4px 0;border-bottom:1px solid rgba(0,0,0,.04);}}
.cs-row:last-child{{border-bottom:none;}}
.cs-name{{font-size:.7rem;font-weight:600;}}
.cs-pills{{display:flex;gap:4px;flex-wrap:wrap;}}
.cs-pill{{font-size:.5rem;letter-spacing:.8px;text-transform:uppercase;font-weight:600;padding:1px 5px;border:1px solid;border-radius:2px;}}
.cs-total{{font-family:'Playfair Display',serif;font-size:.9rem;text-align:right;}}

/* ── AI STRIP ── */
.ai-strip{{background:var(--cream);border-bottom:2px solid var(--border);padding:16px 40px;display:flex;gap:20px;align-items:flex-start;}}
.ai-label{{font-size:.52rem;letter-spacing:3px;text-transform:uppercase;color:var(--gold);font-weight:600;white-space:nowrap;padding-top:2px;flex-shrink:0;border-right:1px solid var(--border);padding-right:16px;line-height:1.6;}}
.ai-body{{flex:1;}}
.sum-head{{font-size:.58rem;letter-spacing:2.5px;text-transform:uppercase;color:var(--accent);font-weight:700;margin:9px 0 2px;}}
.sum-head:first-child{{margin-top:0;}}
.ai-body p{{font-size:.8rem;line-height:1.7;color:var(--ink);font-style:italic;margin-bottom:2px;}}

/* ── TWO-COLUMN LAYOUT ── */
.dash-body{{display:grid;grid-template-columns:1fr var(--sidebar);gap:0;min-height:calc(100vh - 200px);}}
.dash-main{{padding:24px 32px 60px;border-right:1px solid var(--border);}}
.dash-sidebar{{padding:20px 24px 60px;background:var(--cream);}}

/* ── WORK CALENDAR ── */
.work-cal{{margin-bottom:28px;}}
.work-cal-title{{font-size:.58rem;letter-spacing:3px;text-transform:uppercase;font-weight:700;color:var(--accent);border-bottom:2px solid var(--ink);padding-bottom:6px;margin-bottom:12px;}}
.day-cols{{display:grid;grid-template-columns:1fr 1fr 1fr;gap:12px;}}
.day-col{{background:var(--card);border:1px solid var(--border);border-radius:2px;overflow:hidden;}}
.day-col-hdr{{background:rgba(0,0,0,.04);padding:8px 12px;border-bottom:1px solid var(--border);display:flex;justify-content:space-between;align-items:baseline;}}
.day-col-hdr.today-hdr{{background:var(--ink);color:var(--paper);}}
.day-col-hdr strong{{font-size:.72rem;letter-spacing:.5px;}}
.day-col-hdr span{{font-size:.6rem;opacity:.6;}}
.cal-event{{padding:8px 12px;border-bottom:1px solid var(--border);transition:opacity .2s;}}
.cal-event:last-child{{border-bottom:none;}}
.cal-event.past{{opacity:.4;}}
.ev-header{{display:flex;justify-content:space-between;align-items:center;margin-bottom:3px;}}
.ev-time{{font-size:.6rem;letter-spacing:.5px;font-weight:600;color:var(--blue);}}
.ev-title{{font-size:.76rem;font-weight:500;color:var(--ink);line-height:1.35;}}
.ev-loc{{font-size:.62rem;color:var(--muted);margin-top:2px;}}
.teams-link{{font-size:.54rem;letter-spacing:.5px;text-transform:uppercase;font-weight:600;color:white;background:#5b5ea6;padding:2px 6px;text-decoration:none;border-radius:2px;white-space:nowrap;}}
.teams-link:hover{{background:#4a4d8f;}}
.no-meetings{{padding:12px;font-size:.72rem;color:var(--muted);font-style:italic;}}

/* ── TASKS ── */
.dash-section{{margin-bottom:24px;}}
.sec-head{{font-size:.58rem;letter-spacing:3.5px;text-transform:uppercase;font-weight:700;border-bottom:2px solid var(--ink);padding-bottom:7px;margin-bottom:14px;display:flex;align-items:baseline;gap:8px;color:var(--sec-col,var(--accent));}}
.sec-count{{font-family:'Playfair Display',serif;font-size:.75rem;color:var(--muted);letter-spacing:0;font-weight:400;margin-left:auto;}}
.task-grid{{display:grid;grid-template-columns:repeat(auto-fill,minmax(300px,1fr));gap:10px;}}
.task-card{{background:var(--card);border:1px solid var(--border);padding:12px 14px;border-radius:2px;transition:box-shadow .15s;}}
.task-card:hover{{box-shadow:0 2px 10px rgba(0,0,0,.07);}}
.card-header{{display:flex;justify-content:space-between;align-items:flex-start;gap:8px;margin-bottom:6px;}}
.card-title{{font-size:.8rem;font-weight:500;line-height:1.4;flex:1;}}
.card-actions{{display:flex;align-items:center;gap:5px;flex-shrink:0;}}
.card-meta{{display:flex;gap:4px;flex-wrap:wrap;align-items:center;}}
.task-ctx{{font-size:.68rem;color:var(--muted);margin-top:6px;line-height:1.5;border-left:2px solid var(--border);padding-left:7px;}}
.task-list{{display:flex;flex-direction:column;}}
.task-row{{display:flex;justify-content:space-between;align-items:center;gap:10px;padding:7px 0;border-bottom:1px solid var(--border);}}
.task-row:last-child{{border-bottom:none;}}
.task-row-left{{display:flex;align-items:baseline;gap:5px;flex:1;min-width:0;}}
.task-row-title{{font-size:.78rem;font-weight:400;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;}}
.task-row-right{{display:flex;align-items:center;gap:4px;flex-shrink:0;}}
.badge{{font-size:.52rem;letter-spacing:1px;text-transform:uppercase;font-weight:600;padding:2px 5px;border:1px solid;display:inline-block;white-space:nowrap;}}
.badge.overdue{{color:var(--accent);border-color:var(--accent);background:rgba(200,80,42,.07);}}
.badge.today{{color:var(--gold);border-color:var(--gold);background:rgba(176,138,32,.09);}}
.badge.upcoming{{color:var(--muted);border-color:var(--border);}}
.client-tag{{font-size:.52rem;letter-spacing:.8px;text-transform:uppercase;font-weight:600;padding:2px 6px;border:1px solid;display:inline-block;border-radius:2px;white-space:nowrap;}}
.client-tag-sm{{font-size:.5rem;letter-spacing:.8px;text-transform:uppercase;font-weight:600;padding:1px 5px;border:1px solid;display:inline-block;border-radius:2px;white-space:nowrap;}}
.wt-tag{{font-size:.55rem;color:var(--muted);padding:1px 5px;border:1px solid var(--border);white-space:nowrap;}}
.dot{{font-size:.52rem;margin-right:2px;flex-shrink:0;}}
.dot.high{{color:var(--accent);}}.dot.low{{color:var(--border);}}
.open-link{{font-size:.75rem;color:var(--muted);text-decoration:none;opacity:.4;transition:opacity .15s;}}
.open-link:hover{{opacity:1;}}
.done-btn{{font-size:.52rem;letter-spacing:.8px;text-transform:uppercase;font-weight:600;color:var(--green);border:1px solid var(--green);background:rgba(22,163,74,.06);padding:2px 6px;cursor:pointer;transition:all .15s;white-space:nowrap;}}
.done-btn:hover{{background:var(--green);color:white;}}
.done-btn.saving{{color:var(--muted);border-color:var(--border);background:transparent;cursor:default;}}
.done-btn-sm{{font-size:.62rem;font-weight:700;color:var(--green);border:1px solid var(--green);background:rgba(22,163,74,.06);padding:1px 5px;cursor:pointer;transition:all .15s;line-height:1.4;}}
.done-btn-sm:hover{{background:var(--green);color:white;}}
.done-btn-sm.saving{{color:var(--muted);border-color:var(--border);background:transparent;}}
.add-day-btn{{font-size:.52rem;letter-spacing:.8px;text-transform:uppercase;font-weight:600;color:var(--gold);border:1px solid var(--gold);background:rgba(176,138,32,.06);padding:2px 6px;cursor:pointer;transition:all .15s;white-space:nowrap;}}
.add-day-btn:hover{{background:var(--gold);color:var(--ink);}}
.add-day-btn.added{{color:var(--muted);border-color:var(--border);background:transparent;cursor:default;}}
.add-day-btn-sm{{font-size:.62rem;font-weight:700;color:var(--gold);border:1px solid var(--gold);background:rgba(176,138,32,.06);padding:1px 5px;cursor:pointer;transition:all .15s;line-height:1.4;}}
.add-day-btn-sm:hover{{background:var(--gold);color:var(--ink);}}
.add-day-btn-sm.added{{color:var(--muted);border-color:var(--border);background:transparent;}}
.task-card.done-fade,.task-row.done-fade{{opacity:0;transform:translateY(-3px);transition:all .5s ease;pointer-events:none;}}

/* ── SIDEBAR: EMAIL TRIAGE ── */
.sidebar-section{{margin-bottom:24px;}}
.sidebar-title{{font-size:.58rem;letter-spacing:3px;text-transform:uppercase;font-weight:700;border-bottom:2px solid var(--ink);padding-bottom:7px;margin-bottom:12px;display:flex;align-items:baseline;justify-content:space-between;}}
.sidebar-title .stcount{{font-family:'Playfair Display',serif;font-size:.75rem;color:var(--muted);font-weight:400;}}
.email-group-label{{font-size:.54rem;letter-spacing:1.5px;text-transform:uppercase;font-weight:600;color:var(--muted);padding:8px 0 4px;border-bottom:1px dashed var(--border);margin-bottom:6px;}}
.email-group-label:first-child{{padding-top:0;}}
.email-row{{padding:8px 0;border-bottom:1px solid var(--border);}}
.email-row:last-child{{border-bottom:none;}}
.email-header{{display:flex;align-items:center;gap:5px;flex-wrap:wrap;margin-bottom:3px;}}
.email-sender{{font-size:.68rem;font-weight:600;color:var(--ink);}}
.email-date{{font-size:.58rem;color:var(--muted);margin-left:auto;}}
.new-dot{{font-size:.5rem;letter-spacing:1px;text-transform:uppercase;font-weight:700;color:white;background:var(--accent);padding:1px 5px;border-radius:2px;}}
.email-subject{{font-size:.72rem;font-weight:500;line-height:1.35;margin-bottom:3px;}}
.email-subject a{{color:var(--ink);text-decoration:none;}}
.email-subject a:hover{{color:var(--accent);}}
.email-preview{{font-size:.65rem;color:var(--muted);line-height:1.45;}}
.no-emails{{font-size:.72rem;color:var(--muted);font-style:italic;padding:8px 0;}}

/* ── SIDEBAR: FAMILY ── */
.fam-row{{display:flex;gap:10px;align-items:flex-start;padding:7px 0;border-bottom:1px solid var(--border);}}
.fam-row:last-child{{border-bottom:none;}}
.fam-date{{font-size:.65rem;font-weight:600;color:var(--muted);white-space:nowrap;width:56px;flex-shrink:0;padding-top:2px;}}
.fam-events{{display:flex;flex-direction:column;gap:4px;flex:1;}}
.fam-pill{{font-size:.7rem;color:var(--ink);line-height:1.35;}}
.no-family{{font-size:.72rem;color:var(--muted);font-style:italic;padding:8px 0;}}

/* ── MY DAY PANEL ── */
#my-day-panel{{position:fixed;bottom:20px;right:20px;width:320px;background:var(--ink);color:var(--paper);border-radius:4px;box-shadow:0 8px 32px rgba(0,0,0,.35);z-index:1000;transition:all .2s ease;font-family:'DM Sans',sans-serif;}}
#my-day-panel.collapsed{{width:150px;}}
#my-day-header{{display:flex;align-items:center;justify-content:space-between;padding:10px 14px;cursor:pointer;border-bottom:1px solid rgba(255,255,255,.1);user-select:none;}}
#my-day-title{{font-size:.58rem;letter-spacing:2.5px;text-transform:uppercase;font-weight:600;color:var(--gold);}}
#my-day-count{{font-family:'Playfair Display',serif;font-size:.95rem;color:var(--paper);margin-left:8px;}}
#my-day-toggle{{font-size:.68rem;color:rgba(255,255,255,.4);margin-left:auto;}}
#my-day-body{{padding:10px 14px;display:block;}}
#my-day-panel.collapsed #my-day-body{{display:none;}}
#my-day-list{{list-style:none;margin-bottom:10px;min-height:28px;}}
#my-day-list li{{display:flex;align-items:flex-start;gap:7px;padding:6px 0;border-bottom:1px solid rgba(255,255,255,.08);cursor:grab;font-size:.72rem;line-height:1.35;}}
#my-day-list li:last-child{{border-bottom:none;}}
#my-day-list li.dragging{{opacity:.4;}}
#my-day-list li.drag-over{{border-top:2px solid var(--gold);}}
.day-num{{font-family:'Playfair Display',serif;font-size:.78rem;color:var(--gold);flex-shrink:0;width:14px;text-align:right;margin-top:1px;}}
.day-text{{flex:1;}}
.day-client{{font-size:.58rem;color:rgba(255,255,255,.35);display:block;margin-top:1px;}}
.day-remove{{font-size:.68rem;color:rgba(255,255,255,.25);cursor:pointer;flex-shrink:0;padding:0 2px;transition:color .15s;}}
.day-remove:hover{{color:#e07a5a;}}
#my-day-empty{{font-size:.7rem;color:rgba(255,255,255,.3);font-style:italic;padding:4px 0 8px;}}
.day-actions{{display:flex;gap:6px;}}
.day-btn{{flex:1;font-size:.56rem;letter-spacing:1px;text-transform:uppercase;font-weight:600;padding:5px 8px;cursor:pointer;border:none;transition:all .15s;}}
#btn-copy{{background:var(--gold);color:var(--ink);}}
#btn-copy:hover{{background:#c8a020;}}
#btn-clear{{background:rgba(255,255,255,.08);color:rgba(255,255,255,.5);}}
#btn-clear:hover{{background:rgba(255,255,255,.15);color:var(--paper);}}
#copy-confirm{{font-size:.58rem;color:var(--gold);text-align:center;margin-top:6px;height:14px;opacity:0;transition:opacity .3s;}}
#copy-confirm.show{{opacity:1;}}

/* ── FOOTER ── */
.footer{{text-align:center;padding:16px;font-size:.6rem;color:var(--muted);letter-spacing:1px;text-transform:uppercase;border-top:1px solid var(--border);}}

/* ── RESPONSIVE ── */
@media(max-width:1024px){{
  .dash-body{{grid-template-columns:1fr;}}
  .dash-sidebar{{border-top:1px solid var(--border);background:var(--paper);}}
  .day-cols{{grid-template-columns:1fr;}}
  .task-grid{{grid-template-columns:1fr;}}
  .masthead,.stats-bar,.ai-strip,.dash-main,.dash-sidebar,.cs-toggle,.cs-body{{padding-left:20px;padding-right:20px;}}
  #my-day-panel{{width:calc(100vw - 32px);right:16px;bottom:16px;}}
  #my-day-panel.collapsed{{width:140px;}}
}}
</style>
</head>
<body>

<div class="masthead">
  <div class="mast-left">
    <h1>Daily Dashboard</h1>
    <div class="edition">CBRE Copenhagen · Joseph Gilbert</div>
  </div>
  <div class="mast-right">
    <div class="date-line">{today_display}</div>
    <div class="sub">Good morning, Joseph</div>
    <div class="gen-time">Generated {generated_time}</div>
    <button class="refresh-btn" id="refresh-btn" onclick="triggerRefresh()">↻ Refresh</button>
  </div>
</div>

<div class="stats-bar">
  <div class="stat red"><strong>{len(overdue)}</strong><span>Overdue</span></div>
  <div class="stat gold"><strong>{len(today_tasks)}</strong><span>Today</span></div>
  <div class="stat"><strong>{len(waiting)}</strong><span>Waiting</span></div>
  <div class="stat blue"><strong>{len(this_week)}</strong><span>This Week</span></div>
  <div class="stat green"><strong>{new_email_count}</strong><span>New Emails</span></div>
  <div class="stat"><strong>{total}</strong><span>Total Open</span></div>
</div>

{client_sum_html}

<div class="ai-strip">
  <div class="ai-label">✦ AI<br>Briefing</div>
  <div class="ai-body">{summary_html}</div>
</div>

<div class="dash-body">

  <div class="dash-main">

    <!-- Work Calendar -->
    <div class="work-cal">
      <div class="work-cal-title">📅 Work Calendar</div>
      <div class="day-cols">{day_cols}</div>
    </div>

    <!-- Tasks -->
    {task_today_html}
    {task_overdue_html}
    {task_waiting_html}
    {task_week_html}
    {task_later_html}

  </div>

  <div class="dash-sidebar">

    <!-- Email Triage -->
    <div class="sidebar-section">
      <div class="sidebar-title">
        <span>📧 Email Triage</span>
        <span class="stcount">{new_email_count} new</span>
      </div>
      {email_html}
    </div>

    <!-- Family Calendar -->
    <div class="sidebar-section">
      <div class="sidebar-title">
        <span>👨‍👩‍👧 Family</span>
        <span class="stcount">7 days</span>
      </div>
      {family_html}
    </div>

  </div>
</div>

<div class="footer">Generated by GitHub Actions · {today_display} · {total} open tasks · Daily Dashboard v2</div>

<!-- MY DAY PANEL -->
<div id="my-day-panel" class="collapsed">
  <div id="my-day-header" onclick="togglePanel()">
    <span id="my-day-title">✦ My Day</span>
    <span id="my-day-count">0</span>
    <span id="my-day-toggle">▲</span>
  </div>
  <div id="my-day-body">
    <ul id="my-day-list"></ul>
    <p id="my-day-empty">Add tasks with + Day or +</p>
    <div class="day-actions">
      <button class="day-btn" id="btn-copy" onclick="copyList()">Copy List</button>
      <button class="day-btn" id="btn-clear" onclick="clearList()">Clear</button>
    </div>
    <div id="copy-confirm">✓ Copied to clipboard</div>
  </div>
</div>

<script>
  var WORKER_URL = "{WORKER_URL}";
  var items = [];
  var dragSrc = null;

  function togglePanel() {{
    var p = document.getElementById('my-day-panel');
    var t = document.getElementById('my-day-toggle');
    var c = p.classList.toggle('collapsed');
    t.textContent = c ? '▲' : '▼';
  }}

  function addToMyDay(btn) {{
    if (btn.classList.contains('added')) return;
    var title  = btn.getAttribute('data-title');
    var client = btn.getAttribute('data-client');
    if (items.some(function(i) {{ return i.title === title; }})) return;
    items.push({{ title: title, client: client }});
    document.querySelectorAll('[data-title="' + title.replace(/"/g, '\\"') + '"]').forEach(function(el) {{
      if (el.classList.contains('add-day-btn') || el.classList.contains('add-day-btn-sm')) {{
        el.classList.add('added');
        el.textContent = el.classList.contains('add-day-btn-sm') ? '✓' : '✓ Added';
      }}
    }});
    renderList();
    var p = document.getElementById('my-day-panel');
    if (p.classList.contains('collapsed')) {{
      p.classList.remove('collapsed');
      document.getElementById('my-day-toggle').textContent = '▼';
    }}
  }}

  function removeItem(idx) {{
    var removed = items.splice(idx, 1)[0];
    document.querySelectorAll('[data-title="' + removed.title.replace(/"/g, '\\"') + '"]').forEach(function(el) {{
      if (el.classList.contains('add-day-btn') || el.classList.contains('add-day-btn-sm')) {{
        el.classList.remove('added');
        el.textContent = el.classList.contains('add-day-btn-sm') ? '+' : '+ Day';
      }}
    }});
    renderList();
  }}

  function renderList() {{
    var ul = document.getElementById('my-day-list');
    var empty = document.getElementById('my-day-empty');
    document.getElementById('my-day-count').textContent = items.length;
    ul.innerHTML = '';
    if (items.length === 0) {{ empty.style.display = 'block'; return; }}
    empty.style.display = 'none';
    items.forEach(function(item, idx) {{
      var li = document.createElement('li');
      li.draggable = true;
      li.dataset.idx = idx;
      li.innerHTML =
        '<span class="day-num">' + (idx+1) + '</span>' +
        '<span class="day-text">' + escHtml(item.title) +
          (item.client ? '<span class="day-client">' + escHtml(item.client) + '</span>' : '') +
        '</span>' +
        '<span class="day-remove" onclick="removeItem(' + idx + ')">✕</span>';
      li.addEventListener('dragstart', function(e) {{
        dragSrc = idx;
        setTimeout(function() {{ li.classList.add('dragging'); }}, 0);
        e.dataTransfer.effectAllowed = 'move';
      }});
      li.addEventListener('dragend', function() {{ li.classList.remove('dragging'); renderList(); }});
      li.addEventListener('dragover', function(e) {{
        e.preventDefault();
        ul.querySelectorAll('li').forEach(function(l) {{ l.classList.remove('drag-over'); }});
        li.classList.add('drag-over');
      }});
      li.addEventListener('drop', function(e) {{
        e.preventDefault();
        if (dragSrc === null || dragSrc === idx) return;
        var moved = items.splice(dragSrc, 1)[0];
        items.splice(idx, 0, moved);
        dragSrc = null;
        renderList();
      }});
      ul.appendChild(li);
    }});
  }}

  function copyList() {{
    if (items.length === 0) return;
    var today = new Date().toLocaleDateString('en-GB', {{weekday:'long',day:'numeric',month:'long',year:'numeric'}});
    var text = 'My Day — ' + today + '\\n\\n';
    items.forEach(function(item, idx) {{
      text += (idx+1) + '. ' + item.title;
      if (item.client) text += ' [' + item.client + ']';
      text += '\\n';
    }});
    navigator.clipboard.writeText(text).then(function() {{
      var c = document.getElementById('copy-confirm');
      c.classList.add('show');
      setTimeout(function() {{ c.classList.remove('show'); }}, 2000);
    }});
  }}

  function clearList() {{
    items = [];
    document.querySelectorAll('.add-day-btn.added, .add-day-btn-sm.added').forEach(function(btn) {{
      btn.classList.remove('added');
      btn.textContent = btn.classList.contains('add-day-btn-sm') ? '+' : '+ Day';
    }});
    renderList();
  }}

  function markDone(btn) {{
    if (btn.classList.contains('saving')) return;
    var pageId = btn.getAttribute('data-page-id');
    var card   = btn.closest('.task-card, .task-row');
    btn.classList.add('saving');
    btn.textContent = btn.classList.contains('done-btn-sm') ? '…' : 'Saving…';
    fetch(WORKER_URL, {{
      method: 'POST',
      headers: {{ 'Content-Type': 'application/json' }},
      body: JSON.stringify({{ pageId: pageId, action: 'done' }})
    }})
    .then(function(r) {{ return r.json(); }})
    .then(function(data) {{
      if (data.ok) {{
        btn.textContent = btn.classList.contains('done-btn-sm') ? '✓' : '✓ Done';
        if (card) card.classList.add('done-fade');
      }} else {{
        btn.textContent = 'Error'; btn.style.color = 'var(--accent)';
      }}
    }})
    .catch(function() {{ btn.textContent = 'Error'; btn.style.color = 'var(--accent)'; }});
  }}

  function toggleCS() {{
    var body = document.getElementById('cs-body');
    var btn  = document.getElementById('cs-btn');
    var open = body.style.display === 'none';
    body.style.display = open ? 'block' : 'none';
    var span = btn.querySelector('span');
    span.textContent = (open ? '▾ ' : '▸ ') + span.textContent.slice(2);
    btn.querySelector('.cs-hint').textContent = open ? 'click to collapse' : 'click to expand';
  }}

  function triggerRefresh() {{
    var btn = document.getElementById('refresh-btn');
    if (btn.classList.contains('spinning')) return;
    btn.classList.add('spinning');
    btn.textContent = '↻ Refreshing…';
    fetch(WORKER_URL + '/refresh', {{
      method: 'POST',
      headers: {{ 'Content-Type': 'application/json' }},
      body: JSON.stringify({{}})
    }})
    .then(function(r) {{ return r.json(); }})
    .then(function(data) {{
      btn.classList.remove('spinning');
      if (data.ok) {{
        btn.classList.add('done');
        btn.textContent = '✓ Building… reload in ~30s';
        setTimeout(function() {{ btn.classList.remove('done'); btn.textContent = '↻ Refresh'; }}, 35000);
      }} else {{
        btn.classList.add('error'); btn.textContent = '✗ Failed';
        setTimeout(function() {{ btn.classList.remove('error'); btn.textContent = '↻ Refresh'; }}, 4000);
      }}
    }})
    .catch(function() {{
      btn.classList.remove('spinning'); btn.classList.add('error'); btn.textContent = '✗ Failed';
      setTimeout(function() {{ btn.classList.remove('error'); btn.textContent = '↻ Refresh'; }}, 4000);
    }});
  }}

  function escHtml(s) {{
    return (s||'').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
  }}
</script>
</body>
</html>"""


# ── MAIN ──────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    today_str = date.today().isoformat()
    print(f"\n{'='*60}")
    print(f"Daily Dashboard — {today_str}")
    print(f"{'='*60}")

    print("\nBuilding client map...")
    client_map = build_client_map()

    print("\nFetching tasks...")
    tasks = fetch_tasks(client_map)

    print("\nBucketing tasks...")
    overdue, today_tasks, waiting, this_week, later = bucket_tasks(tasks, today_str)
    print(f"  Today:{len(today_tasks)} Overdue:{len(overdue)} Waiting:{len(waiting)} Week:{len(this_week)} Later:{len(later)}")

    print("\nFetching calendar data...")
    work_days, family_events = fetch_all_calendar_data(today_str)

    print("\nFetching emails...")
    emails = fetch_emails(days_back=5)

    print("\nGenerating AI summary...")
    summary = generate_summary(overdue, today_tasks, waiting, this_week, work_days, emails, today_str)
    print(f"  Done ({len(summary)} chars)")

    print("\nBuilding HTML...")
    html = build_html(overdue, today_tasks, waiting, this_week, later, summary,
                      work_days, family_events, emails, today_str)

    with open("index.html", "w", encoding="utf-8") as f:
        f.write(html)

    print(f"  index.html written ({len(html):,} bytes)")
    print(f"{'='*60}\n")
