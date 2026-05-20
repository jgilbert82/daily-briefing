"""
generate.py — fetches tasks from Notion, calls Claude for a focused summary,
and writes index.html for the Daily Briefing GitHub Pages site.

Key changes from v1:
- Filters out Done tasks (Status=Done or Done checkbox)
- Uses Horizon field (Today/This Week/Next Week/Someday)
- Resolves Client relation to actual names
- Deduplicates repeated task titles (e.g. "Approve invoices" templates)
- Shows AI email summaries (Message field) as task context
- Sections: Today → Overdue → Waiting → This Week → Later
"""

import os
import re
import requests
from datetime import date, datetime
import anthropic
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from googleapiclient.discovery import build

# ── CONFIG ────────────────────────────────────────────────────────────────────

NOTION_TASKS_DB   = "d5669af6-6732-432b-b1ac-558a860164ca"
NOTION_CLIENTS_DB = "81b6953304db4b76ba7a25a9704fe7b2"
NOTION_VERSION    = "2022-06-28"

# Your Cloudflare Worker URL — fill in after deploying worker.js
WORKER_URL = "https://notion-proxy.jgilbert82.workers.dev"

# Google Calendar — work calendar (Outlook ICS imported to Google Calendar)
WORK_CALENDAR_ID = "86iqekmmn19f3b7j1r9ihepkt9ethdtc@import.calendar.google.com"

CLIENT_COLOURS = {
    "AEW":           "#2563eb",
    "SSCP":          "#16a34a",
    "Ingka":         "#d97706",
    "Hedeland":      "#d97706",
    "Mileway":       "#7c3aed",
    "AXA":           "#0891b2",
    "Arrow":         "#dc2626",
    "EQT":           "#be185d",
    "M&G":           "#065f46",
    "BNPP":          "#1d4ed8",
    "CBRE Internal": "#64748b",
}

WORK_TYPE_ICONS = {
    "Finance / SC":    "💰",
    "Reporting":       "📊",
    "Chasing":         "📧",
    "Client Relations":"🤝",
    "Internal":        "🏢",
    "Legal":           "⚖️",
}


def notion_headers():
    return {
        "Authorization": f"Bearer {os.environ['NOTION_API_KEY']}",
        "Content-Type": "application/json",
        "Notion-Version": NOTION_VERSION,
    }



# ── GOOGLE CALENDAR ───────────────────────────────────────────────────────────

def fetch_calendar_events(today_str):
    """Fetch today's events from the work calendar via Google Calendar API."""
    import json
    try:
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

        service = build("calendar", "v3", credentials=creds)

        # Query today midnight → tonight midnight (Copenhagen = UTC+2 in summer)
        time_min = today_str + "T00:00:00+02:00"
        time_max = today_str + "T23:59:59+02:00"

        result = service.events().list(
            calendarId=WORK_CALENDAR_ID,
            timeMin=time_min,
            timeMax=time_max,
            singleEvents=True,
            orderBy="startTime",
            maxResults=20,
        ).execute()

        events = []
        for item in result.get("items", []):
            start = item.get("start", {})
            end   = item.get("end",   {})
            # Skip all-day events (they have "date" not "dateTime")
            if "dateTime" not in start:
                continue
            from datetime import datetime as dt
            import re
            def parse_time(s):
                # Parse ISO datetime and return HH:MM in Copenhagen time
                try:
                    import re
                    d = dt.fromisoformat(s)
                    # If datetime has timezone info, convert to Copenhagen (UTC+2 summer)
                    if d.tzinfo is not None:
                        from datetime import timezone, timedelta
                        copenhagen = timezone(timedelta(hours=2))
                        d = d.astimezone(copenhagen)
                    return d.strftime("%H:%M")
                except Exception:
                    return s[:5]

            summary  = item.get("summary", "Untitled")
            location = item.get("location", "") or ""
            start_t  = parse_time(start["dateTime"])
            end_t    = parse_time(end["dateTime"])

            # Skip declined events
            attendees = item.get("attendees", [])
            declined  = any(
                a.get("self") and a.get("responseStatus") == "declined"
                for a in attendees
            )
            if declined:
                continue

            events.append({
                "summary":  summary,
                "start":    start_t,
                "end":      end_t,
                "location": location[:60],
            })

        print(f"  Calendar: {len(events)} events today")
        return events

    except Exception as e:
        print(f"  WARNING: Calendar fetch failed: {e}")
        return []

# ── CLIENT LOOKUP ─────────────────────────────────────────────────────────────

def build_client_map():
    """Fetch all pages in Client Notes DB and return {page_id: client_name}."""
    client_map = {}
    cursor = None
    while True:
        body = {"page_size": 100}
        if cursor:
            body["start_cursor"] = cursor
        resp = requests.post(
            f"https://api.notion.com/v1/databases/{NOTION_CLIENTS_DB}/query",
            headers=notion_headers(),
            json=body,
        )
        resp.raise_for_status()
        data = resp.json()
        for page in data["results"]:
            page_id = page["id"]
            page_id_plain = page_id.replace("-", "")
            props = page["properties"]
            # Try common title property names
            title_parts = (
                props.get("Client", {}).get("title", [])
                or props.get("Name",   {}).get("title", [])
                or props.get("name",   {}).get("title", [])
            )
            name = "".join(p.get("plain_text", "") for p in title_parts).strip()
            if name:
                client_map[page_id]       = name
                client_map[page_id_plain] = name
        if not data.get("has_more"):
            break
        cursor = data["next_cursor"]
    print(f"  Client map: {len(client_map)//2} clients found")
    return client_map


# ── NOTION FETCH ──────────────────────────────────────────────────────────────

def parse_client_name(client_prop, client_map):
    """Extract client name from a Notion relation property."""
    relations = client_prop.get("relation", [])
    if not relations:
        return None
    pid = relations[0].get("id", "")
    return client_map.get(pid) or client_map.get(pid.replace("-", "")) or None


def is_done(props):
    """Return True if task is completed."""
    status = (props.get("Status", {}).get("select") or {}).get("name", "")
    if status == "Done":
        return True
    return bool(props.get("Done", {}).get("checkbox", False))


def fetch_all_tasks(client_map):
    all_tasks = []
    seen_titles = {}
    cursor = None

    while True:
        body = {"page_size": 100}
        if cursor:
            body["start_cursor"] = cursor

        resp = requests.post(
            f"https://api.notion.com/v1/databases/{NOTION_TASKS_DB}/query",
            headers=notion_headers(),
            json=body,
        )
        resp.raise_for_status()
        data = resp.json()

        for page in data["results"]:
            props = page["properties"]

            # Skip done tasks
            if is_done(props):
                continue

            # Title
            title_parts = props.get("Task", {}).get("title", [])
            title = "".join(p.get("plain_text", "") for p in title_parts).strip()
            if not title:
                continue

            # Deduplicate repeated titles (template noise like "Approve invoices")
            seen_titles[title] = seen_titles.get(title, 0) + 1
            if seen_titles[title] > 1:
                continue

            status    = (props.get("Status",    {}).get("select") or {}).get("name", "") or "Not Started"
            horizon   = (props.get("Horizon",   {}).get("select") or {}).get("name", "") or ""
            priority  = (props.get("Priority",  {}).get("select") or {}).get("name", "") or ""
            work_type = (props.get("Work Type", {}).get("select") or {}).get("name", "") or ""
            source    = (props.get("Source",    {}).get("select") or {}).get("name", "") or ""

            due_obj = props.get("Due Date", {}).get("date") or {}
            due_str = (due_obj.get("start") or "")[:10] or None

            client_name = parse_client_name(props.get("Client", {}), client_map)

            # Context: AI email Message summary preferred, fallback to Notes
            message = "".join(
                p.get("plain_text", "")
                for p in props.get("Message", {}).get("rich_text", [])
            ).strip()
            notes = "".join(
                p.get("plain_text", "")
                for p in props.get("Notes", {}).get("rich_text", [])
            ).strip()
            context = (message or notes or "")[:250] or None

            # Sender name (strip email address)
            sender_raw = "".join(
                p.get("plain_text", "")
                for p in (props.get("Sender", {}).get("rich_text", []) or [])
            ).strip()
            sender = sender_raw.split("<")[0].strip() if sender_raw else None

            all_tasks.append({
                "title":     title,
                "status":    status,
                "horizon":   horizon,
                "due":       due_str,
                "priority":  priority,
                "work_type": work_type,
                "source":    source,
                "client":    client_name,
                "context":   context,
                "sender":    sender,
                "id":        page["id"],
                "url":       page["url"],
            })

        if not data.get("has_more"):
            break
        cursor = data["next_cursor"]

    print(f"  {len(all_tasks)} active (non-done) tasks fetched")
    return all_tasks


# ── BUCKETING ─────────────────────────────────────────────────────────────────

def bucket_tasks(tasks, today_str):
    overdue, today, waiting, this_week, later = [], [], [], [], []

    for t in tasks:
        if t["status"] == "Waiting":
            waiting.append(t)
            continue

        due     = t["due"]
        horizon = t["horizon"]

        if due and due < today_str:
            overdue.append(t)
        elif horizon == "🔴 Today" or due == today_str:
            today.append(t)
        elif horizon == "🟡 This Week":
            this_week.append(t)
        else:
            later.append(t)

    def sort_key(t):
        p = {"High": 0, "Medium": 1, "Low": 2}.get(t["priority"], 3)
        d = t["due"] or "9999-99-99"
        return (p, d)

    for bucket in [overdue, today, waiting, this_week, later]:
        bucket.sort(key=sort_key)

    return overdue, today, waiting, this_week, later


# ── AI SUMMARY ────────────────────────────────────────────────────────────────

def generate_summary(overdue, today, waiting, this_week, today_str, calendar_events=None):
    client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
    today_date = date.today().strftime("%A %-d %B %Y")

    def fmt(tasks, limit=10):
        return "\n".join(
            f"  [{t.get('client') or 'No client'}] [{t.get('work_type') or ''}] "
            f"[{t['priority'] or ''}] {t['title']}"
            + (f" (due {t['due']})" if t["due"] else "")
            for t in tasks[:limit]
        )

    sections = []
    if overdue:
        sections.append(f"OVERDUE ({len(overdue)}):\n{fmt(overdue, 8)}")
    if today:
        sections.append(f"TODAY ({len(today)}):\n{fmt(today)}")
    if waiting:
        sections.append(f"WAITING ({len(waiting)}):\n{fmt(waiting, 6)}")
    if this_week:
        sections.append(f"THIS WEEK ({len(this_week)}):\n{fmt(this_week, 8)}")

    # Add calendar context
    if calendar_events:
        cal_lines = "\n".join(
            f"  {e['start']}–{e['end']} {e['summary']}" + (f" @ {e['location']}" if e['location'] else "")
            for e in calendar_events
        )
        sections.insert(0, f"TODAY'S MEETINGS:\n{cal_lines}")

    task_text = "\n\n".join(sections) or "No active tasks."

    msg = client.messages.create(
        model="claude-opus-4-5",
        max_tokens=450,
        system=(
            f"You are a sharp executive assistant briefing Joseph, Senior Property Manager "
            f"at CBRE Copenhagen. Clients: AEW (Sydmarken & Kystvejen), SSCP, Ingka/Hedeland, "
            f"Mileway, AXA Nordic, Arrow Capital, EQT, M&G. Today is {today_date}.\n\n"
            f"Write a tight morning briefing with EXACTLY these 3 section headings on their own line:\n"
            f"MUST DO TODAY\n"
            f"WATCH / CHASING\n"
            f"THIS WEEK\n\n"
            f"Be SPECIFIC — name the actual tasks and clients. Reference today's meetings where relevant. 2-3 sentences per section. "
            f"No bullets. Plain prose."
        ),
        messages=[{"role": "user", "content": task_text}],
    )
    return msg.content[0].text.strip()


# ── HELPERS ───────────────────────────────────────────────────────────────────

def esc(s):
    return (s or "").replace("&","&amp;").replace("<","&lt;").replace(">","&gt;").replace('"','&quot;')

def fmt_date(due):
    try:
        return datetime.strptime(due, "%Y-%m-%d").strftime("%-d %b")
    except Exception:
        return due

def clean_title(title):
    title = re.sub(r'^Day Book[^:]+:\s*', '', title)
    title = re.sub(r'^Inbox\s*:\s*', '', title)
    return title.strip()

def client_colour(name):
    if not name:
        return "#64748b"
    for key, col in CLIENT_COLOURS.items():
        if key.lower() in name.lower():
            return col
    return "#64748b"

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



# ── CLIENT SUMMARY ────────────────────────────────────────────────────────────

def render_client_summary(tasks):
    """Render a collapsible client breakdown showing task counts by status."""
    from collections import defaultdict

    # Build per-client counts
    client_data = defaultdict(lambda: {"total": 0, "overdue": 0, "today": 0, "waiting": 0, "high": 0})
    no_client = {"total": 0, "overdue": 0, "today": 0, "waiting": 0, "high": 0}

    today_str = __import__("datetime").date.today().isoformat()

    for t in tasks:
        c = t.get("client") or ""
        d = client_data[c] if c else no_client
        d["total"] += 1
        if t["status"] == "Waiting":
            d["waiting"] += 1
        due = t.get("due") or ""
        if due and due < today_str:
            d["overdue"] += 1
        elif due == today_str:
            d["today"] += 1
        if t.get("priority") == "High":
            d["high"] += 1

    # Sort by total desc
    rows = sorted(client_data.items(), key=lambda x: -x[1]["total"])

    def colour(name):
        CLIENT_COLOURS_LOCAL = {
            "AEW": "#2563eb", "SSCP": "#16a34a", "Ingka": "#d97706",
            "Hedeland": "#d97706", "Mileway": "#7c3aed", "AXA": "#0891b2",
            "Arrow": "#dc2626", "EQT": "#be185d", "M&G": "#065f46",
            "BNPP": "#1d4ed8", "CBRE Internal": "#64748b",
        }
        for k, v in CLIENT_COLOURS_LOCAL.items():
            if k.lower() in (name or "").lower():
                return v
        return "#64748b"

    def pill(label, value, col="#64748b"):
        if not value:
            return ""
        return f'<span class="cs-pill" style="background:{col}18;color:{col};border-color:{col}44">{label} {value}</span>'

    rows_html = ""
    for name, d in rows:
        if not name:
            continue
        col = colour(name)
        pills = ""
        if d["overdue"]:
            pills += pill("Overdue", d["overdue"], "#c8502a")
        if d["today"]:
            pills += pill("Today", d["today"], "#b08a20")
        if d["waiting"]:
            pills += pill("Waiting", d["waiting"], "#7a7468")
        if d["high"]:
            pills += pill("High", d["high"], "#c8502a")

        rows_html += f'''<div class="cs-row">
  <div class="cs-name" style="color:{col}">{esc(name)}</div>
  <div class="cs-pills">{pills}</div>
  <div class="cs-total" style="color:{col}">{d["total"]}</div>
</div>'''

    # Add no-client row if any
    if no_client["total"]:
        pills = ""
        if no_client["overdue"]:
            pills += pill("Overdue", no_client["overdue"], "#c8502a")
        if no_client["waiting"]:
            pills += pill("Waiting", no_client["waiting"], "#7a7468")
        rows_html += f'''<div class="cs-row">
  <div class="cs-name" style="color:var(--muted)">No client</div>
  <div class="cs-pills">{pills}</div>
  <div class="cs-total" style="color:var(--muted)">{no_client["total"]}</div>
</div>'''

    total_clients = len([r for r in rows if r[0]])
    return f'''<div class="client-summary" id="cs-wrapper">
  <button class="cs-toggle" onclick="toggleCS()" id="cs-btn">
    <span>▸ Clients ({total_clients})</span>
    <span class="cs-hint">click to expand</span>
  </button>
  <div class="cs-body" id="cs-body" style="display:none">
    <div class="cs-header">
      <span>Client</span><span></span><span style="text-align:right">Open</span>
    </div>
    {rows_html}
  </div>
</div>'''

# ── RENDERING ─────────────────────────────────────────────────────────────────

def render_task_card(t, today_str, compact=False):
    title    = clean_title(t["title"])
    due      = t["due"]
    client   = t["client"]
    context  = t["context"]
    sender   = t["sender"]

    # Due badge
    if due and due < today_str:
        due_badge = f'<span class="badge overdue">{fmt_date(due)}</span>'
    elif due == today_str:
        due_badge = '<span class="badge today">Today</span>'
    elif due:
        due_badge = f'<span class="badge upcoming">{fmt_date(due)}</span>'
    else:
        due_badge = ""

    # Priority dot
    pri_dot = {"High": '<span class="dot high">●</span>', "Low": '<span class="dot low">●</span>'}.get(t["priority"], "")

    # Client badge
    col = client_colour(client)
    client_badge = (
        f'<span class="client-tag" style="background:{col}18;color:{col};border-color:{col}44">{esc(client)}</span>'
        if client else ""
    )

    # Work type
    wt_icon = WORK_TYPE_ICONS.get(t["work_type"], "")
    wt_badge = f'<span class="wt-tag">{wt_icon} {esc(t["work_type"])}</span>' if t["work_type"] else ""

    # Status
    status_badge = '<span class="status-inprogress">In Progress</span>' if t["status"] == "In Progress" else ""

    # Context snippet
    ctx_html = ""
    if context and not compact:
        prefix = f"<em>{esc(sender)}:</em> " if sender else ""
        snippet = esc(context[:180] + ("…" if len(context) > 180 else ""))
        ctx_html = f'<div class="task-ctx">{prefix}{snippet}</div>'

    page_id   = t["id"]
    open_link = '<a class="open-link" href="' + esc(t['url']) + '" target="_blank">↗</a>'
    add_btn   = '<button class="add-day-btn" onclick="addToMyDay(this)" data-title="' + esc(title) + '" data-client="' + esc(client or '') + '">+ My Day</button>'
    done_btn  = '<button class="done-btn" onclick="markDone(this)" data-page-id="' + page_id + '">✓ Done</button>'

    if compact:
        return (
            f'<div class="task-row" data-page-id="{page_id}">'
            f'<div class="task-row-left">{pri_dot}<span class="task-row-title">{esc(title)}</span></div>'
            f'<div class="task-row-right">{due_badge}{client_badge}'
            f'<button class="done-btn-sm" onclick="markDone(this)" data-page-id="{page_id}">✓</button>'
            f'<button class="add-day-btn-sm" onclick="addToMyDay(this)" data-title="{esc(title)}" data-client="{esc(client or "")}">+</button>'
            f'{open_link}</div>'
            f'</div>'
        )
    return (
        f'<div class="task-card" data-page-id="{page_id}">'
        f'<div class="card-header"><div class="card-title">{pri_dot} {esc(title)}</div>'
        f'<div class="card-actions">{done_btn}{add_btn}{open_link}</div></div>'
        f'<div class="card-meta">{due_badge}{status_badge}{client_badge}{wt_badge}</div>'
        f'{ctx_html}'
        f'</div>'
    )


def render_section(heading, icon, tasks, today_str, compact=False, colour="var(--accent)"):
    if not tasks:
        return ""
    items = "\n".join(render_task_card(t, today_str, compact=compact) for t in tasks)
    grid_class = "task-list" if compact else "task-grid"
    return (
        f'<section class="briefing-section">'
        f'<div class="sec-head" style="color:{colour}">'
        f'<span>{icon} {heading}</span><span class="sec-count">{len(tasks)}</span>'
        f'</div>'
        f'<div class="{grid_class}">{items}</div>'
        f'</section>'
    )



# ── CALENDAR RENDER ───────────────────────────────────────────────────────────

def render_calendar_strip(events):
    """Render today's meetings as a horizontal strip above the AI summary."""
    if not events:
        return '<div class="cal-strip"><div class="cal-label">📅 Meetings</div><div class="cal-empty">No meetings scheduled today</div></div>'

    cards = ""
    for e in events:
        loc = f'<div class="cal-loc">📍 {esc(e["location"])}</div>' if e["location"] else ""
        cards += f'''<div class="cal-event">
  <div class="cal-time">{esc(e["start"])}–{esc(e["end"])}</div>
  <div class="cal-title">{esc(e["summary"])}</div>
  {loc}
</div>'''

    return f'<div class="cal-strip"><div class="cal-label">📅 Meetings</div><div class="cal-events">{cards}</div></div>'

# ── HTML ──────────────────────────────────────────────────────────────────────

def build_html(overdue, today, waiting, this_week, later, summary, today_str, calendar_events=None):
    today_display   = datetime.strptime(today_str, "%Y-%m-%d").strftime("%A %-d %B %Y")
    calendar_strip       = render_calendar_strip(calendar_events or [])
    all_tasks            = overdue + today + waiting + this_week + later
    client_summary_html  = render_client_summary(all_tasks)
    generated_time  = datetime.utcnow().strftime("%H:%M UTC")
    total          = len(overdue) + len(today) + len(waiting) + len(this_week) + len(later)

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Daily Briefing · {today_display}</title>
<link href="https://fonts.googleapis.com/css2?family=Playfair+Display:ital,wght@0,400;0,700;1,400&family=DM+Sans:opsz,wght@9..40,300;9..40,400;9..40,500&display=swap" rel="stylesheet">
<style>
:root{{--ink:#1a1a2e;--paper:#f5f0e8;--cream:#ede8dc;--accent:#c8502a;--gold:#b08a20;--muted:#7a7468;--border:#d4cfc5;--card:#faf7f2;}}
*{{box-sizing:border-box;margin:0;padding:0;}}
body{{font-family:'DM Sans',sans-serif;background:var(--paper);color:var(--ink);min-height:100vh;}}
.masthead{{background:var(--ink);color:var(--paper);padding:24px 48px 20px;display:flex;align-items:flex-end;justify-content:space-between;flex-wrap:wrap;gap:12px;border-bottom:4px solid var(--accent);}}
.masthead h1{{font-family:'Playfair Display',serif;font-size:2.4rem;letter-spacing:-0.5px;line-height:1;}}
.edition{{font-size:.65rem;letter-spacing:3px;text-transform:uppercase;color:var(--accent);margin-top:6px;font-weight:500;}}
.mast-right{{text-align:right;}}
.date-line{{font-family:'Playfair Display',serif;font-size:1rem;opacity:.85;}}
.sub{{font-size:.65rem;letter-spacing:2px;text-transform:uppercase;opacity:.4;margin-top:4px;}}
.gen-time{{font-size:.58rem;letter-spacing:1px;text-transform:uppercase;color:rgba(255,255,255,.25);margin-top:3px;}}
.refresh-btn{{font-size:.55rem;letter-spacing:1.5px;text-transform:uppercase;font-weight:600;
  color:rgba(255,255,255,.4);border:1px solid rgba(255,255,255,.15);background:transparent;
  padding:3px 10px;cursor:pointer;margin-top:6px;transition:all .2s;display:inline-block;}}
.refresh-btn:hover{{color:var(--paper);border-color:rgba(255,255,255,.5);background:rgba(255,255,255,.08);}}
.refresh-btn.spinning{{color:rgba(255,255,255,.25);border-color:rgba(255,255,255,.1);cursor:default;}}
.refresh-btn.done{{color:#4ade80;border-color:#4ade8055;}}
.refresh-btn.error{{color:#e07a5a;border-color:#e07a5a55;}}
.stats-bar{{background:var(--ink);display:flex;padding:14px 48px;border-top:1px solid rgba(255,255,255,.08);flex-wrap:wrap;gap:28px;}}
.stat strong{{font-family:'Playfair Display',serif;font-size:1.7rem;display:block;line-height:1;color:var(--paper);}}
.stat span{{font-size:.58rem;letter-spacing:2.5px;text-transform:uppercase;color:rgba(255,255,255,.3);}}
.stat.red strong{{color:#e07a5a;}}.stat.gold strong{{color:#d4aa50;}}.stat.blue strong{{color:#93c5fd;}}
.ai-strip{{background:var(--cream);border-bottom:2px solid var(--border);padding:20px 48px;display:flex;gap:24px;align-items:flex-start;}}
.ai-label{{font-size:.55rem;letter-spacing:3px;text-transform:uppercase;color:var(--gold);font-weight:600;white-space:nowrap;padding-top:2px;flex-shrink:0;line-height:1.6;border-right:1px solid var(--border);padding-right:20px;}}
.ai-body{{flex:1;}}
.sum-head{{font-size:.6rem;letter-spacing:2.5px;text-transform:uppercase;color:var(--accent);font-weight:700;margin:10px 0 3px;}}
.sum-head:first-child{{margin-top:0;}}
.ai-body p{{font-size:.82rem;line-height:1.7;color:var(--ink);font-style:italic;margin-bottom:2px;}}
.main{{padding:0 48px 60px;}}
.briefing-section{{margin-top:32px;}}
.sec-head{{font-size:.62rem;letter-spacing:3.5px;text-transform:uppercase;font-weight:700;border-bottom:2px solid var(--ink);padding-bottom:8px;margin-bottom:16px;display:flex;align-items:baseline;gap:8px;}}
.sec-count{{font-family:'Playfair Display',serif;font-size:.8rem;color:var(--muted);letter-spacing:0;font-weight:400;margin-left:auto;}}
.task-grid{{display:grid;grid-template-columns:repeat(auto-fill,minmax(340px,1fr));gap:12px;}}
.task-card{{background:var(--card);border:1px solid var(--border);padding:14px 16px;border-radius:2px;transition:box-shadow .15s;}}
.task-card:hover{{box-shadow:0 2px 12px rgba(0,0,0,.08);}}
.card-header{{display:flex;justify-content:space-between;align-items:flex-start;gap:8px;margin-bottom:7px;}}
.card-title{{font-size:.82rem;font-weight:500;line-height:1.45;color:var(--ink);flex:1;}}
.open-link{{font-size:.8rem;color:var(--muted);text-decoration:none;opacity:.45;flex-shrink:0;transition:opacity .15s;padding:0 2px;}}
.open-link:hover{{opacity:1;color:var(--ink);}}
.card-meta{{display:flex;gap:5px;flex-wrap:wrap;align-items:center;}}
.task-ctx{{font-size:.7rem;color:var(--muted);margin-top:7px;line-height:1.55;border-left:2px solid var(--border);padding-left:8px;}}
.task-ctx em{{font-style:normal;font-weight:500;color:var(--ink);opacity:.7;}}
.task-list{{display:flex;flex-direction:column;}}
.task-row{{display:flex;justify-content:space-between;align-items:center;gap:12px;padding:8px 0;border-bottom:1px solid var(--border);}}
.task-row:last-child{{border-bottom:none;}}
.task-row-left{{display:flex;align-items:baseline;gap:6px;flex:1;min-width:0;}}
.task-row-title{{font-size:.8rem;font-weight:400;color:var(--ink);white-space:nowrap;overflow:hidden;text-overflow:ellipsis;}}
.task-row-right{{display:flex;align-items:center;gap:5px;flex-shrink:0;}}
.badge{{font-size:.54rem;letter-spacing:1px;text-transform:uppercase;font-weight:600;padding:2px 6px;border:1px solid;display:inline-block;white-space:nowrap;}}
.badge.overdue{{color:var(--accent);border-color:var(--accent);background:rgba(200,80,42,.07);}}
.badge.today{{color:var(--gold);border-color:var(--gold);background:rgba(176,138,32,.09);}}
.badge.upcoming{{color:var(--muted);border-color:var(--border);}}
.client-tag{{font-size:.54rem;letter-spacing:.8px;text-transform:uppercase;font-weight:600;padding:2px 7px;border:1px solid;display:inline-block;border-radius:2px;white-space:nowrap;}}
.wt-tag{{font-size:.58rem;color:var(--muted);padding:1px 5px;border:1px solid var(--border);background:rgba(0,0,0,.025);white-space:nowrap;}}
.status-inprogress{{font-size:.54rem;letter-spacing:.5px;text-transform:uppercase;color:#0891b2;border:1px solid #0891b244;padding:2px 6px;background:#0891b209;}}
.dot{{font-size:.55rem;margin-right:2px;line-height:1;flex-shrink:0;}}
.dot.high{{color:var(--accent);}}.dot.low{{color:var(--border);}}
.client-summary{{background:var(--cream);border-bottom:1px solid var(--border);}}
.cs-toggle{{width:100%;background:none;border:none;padding:10px 48px;display:flex;align-items:center;justify-content:space-between;cursor:pointer;font-size:.6rem;letter-spacing:2.5px;text-transform:uppercase;font-weight:600;color:var(--ink);transition:background .15s;text-align:left;}}
.cs-toggle:hover{{background:rgba(0,0,0,.03);}}
.cs-hint{{font-size:.55rem;letter-spacing:1px;color:var(--muted);font-weight:400;text-transform:none;}}
.cs-body{{padding:0 48px 14px;}}
.cs-header{{display:grid;grid-template-columns:160px 1fr 40px;font-size:.54rem;letter-spacing:2px;text-transform:uppercase;color:var(--muted);padding:6px 0;border-bottom:1px solid var(--border);margin-bottom:4px;}}
.cs-row{{display:grid;grid-template-columns:160px 1fr 40px;align-items:center;padding:5px 0;border-bottom:1px solid rgba(0,0,0,.04);}}
.cs-row:last-child{{border-bottom:none;}}
.cs-name{{font-size:.72rem;font-weight:600;letter-spacing:.5px;}}
.cs-pills{{display:flex;gap:5px;flex-wrap:wrap;}}
.cs-pill{{font-size:.52rem;letter-spacing:.8px;text-transform:uppercase;font-weight:600;padding:2px 6px;border:1px solid;border-radius:2px;white-space:nowrap;}}
.cs-total{{font-family:'Playfair Display',serif;font-size:1rem;text-align:right;font-weight:400;}}
.cal-strip{{background:#f0f4ff;border-bottom:2px solid var(--border);padding:14px 48px;display:flex;align-items:flex-start;gap:20px;}}
.cal-label{{font-size:.55rem;letter-spacing:3px;text-transform:uppercase;color:#2563eb;font-weight:600;white-space:nowrap;padding-top:4px;flex-shrink:0;border-right:1px solid var(--border);padding-right:20px;}}
.cal-events{{display:flex;gap:10px;flex-wrap:wrap;flex:1;}}
.cal-event{{background:white;border:1px solid #dbeafe;border-left:3px solid #2563eb;padding:8px 12px;border-radius:2px;min-width:140px;}}
.cal-time{{font-size:.62rem;letter-spacing:.5px;font-weight:600;color:#2563eb;margin-bottom:3px;}}
.cal-title{{font-size:.78rem;font-weight:500;color:var(--ink);line-height:1.3;}}
.cal-loc{{font-size:.62rem;color:var(--muted);margin-top:3px;}}
.cal-empty{{font-size:.75rem;color:var(--muted);font-style:italic;padding-top:3px;}}
.footer{{text-align:center;padding:20px;font-size:.62rem;color:var(--muted);letter-spacing:1px;text-transform:uppercase;border-top:1px solid var(--border);}}
.card-actions{{display:flex;align-items:center;gap:6px;flex-shrink:0;}}
.add-day-btn{{font-size:.54rem;letter-spacing:.8px;text-transform:uppercase;font-weight:600;
  color:var(--gold);border:1px solid var(--gold);background:rgba(176,138,32,.06);
  padding:2px 7px;cursor:pointer;white-space:nowrap;transition:all .15s;}}
.add-day-btn:hover{{background:var(--gold);color:var(--paper);}}
.add-day-btn.added{{color:var(--muted);border-color:var(--border);background:transparent;cursor:default;}}
.add-day-btn-sm{{font-size:.65rem;font-weight:700;color:var(--gold);border:1px solid var(--gold);
  background:rgba(176,138,32,.06);padding:1px 6px;cursor:pointer;transition:all .15s;line-height:1.4;}}
.add-day-btn-sm:hover{{background:var(--gold);color:var(--paper);}}
.add-day-btn-sm.added{{color:var(--border);border-color:var(--border);background:transparent;cursor:default;}}
.done-btn{{font-size:.54rem;letter-spacing:.8px;text-transform:uppercase;font-weight:600;color:#16a34a;border:1px solid #16a34a;background:rgba(22,163,74,.06);padding:2px 7px;cursor:pointer;white-space:nowrap;transition:all .15s;}}
.done-btn:hover{{background:#16a34a;color:var(--paper);}}
.done-btn.saving{{color:var(--muted);border-color:var(--border);cursor:default;background:transparent;}}
.done-btn-sm{{font-size:.65rem;font-weight:700;color:#16a34a;border:1px solid #16a34a;background:rgba(22,163,74,.06);padding:1px 6px;cursor:pointer;transition:all .15s;line-height:1.4;}}
.done-btn-sm:hover{{background:#16a34a;color:var(--paper);}}
.done-btn-sm.saving{{color:var(--muted);border-color:var(--border);background:transparent;cursor:default;}}
.task-card.done-fade,.task-row.done-fade{{opacity:0;transform:translateY(-3px);transition:all .5s ease;pointer-events:none;}}

/* ── MY DAY PANEL ── */
#my-day-panel{{
  position:fixed;bottom:24px;right:24px;width:340px;
  background:var(--ink);color:var(--paper);border-radius:4px;
  box-shadow:0 8px 32px rgba(0,0,0,.35);z-index:1000;
  transition:all .2s ease;font-family:'DM Sans',sans-serif;
}}
#my-day-panel.collapsed{{width:160px;}}
#my-day-header{{
  display:flex;align-items:center;justify-content:space-between;
  padding:12px 16px;cursor:pointer;border-bottom:1px solid rgba(255,255,255,.1);
  user-select:none;
}}
#my-day-title{{font-size:.6rem;letter-spacing:2.5px;text-transform:uppercase;font-weight:600;color:var(--gold);}}
#my-day-count{{font-family:'Playfair Display',serif;font-size:1rem;color:var(--paper);margin-left:8px;}}
#my-day-toggle{{font-size:.7rem;color:rgba(255,255,255,.4);margin-left:auto;}}
#my-day-body{{padding:12px 16px;display:block;}}
#my-day-panel.collapsed #my-day-body{{display:none;}}
#my-day-list{{list-style:none;margin-bottom:12px;min-height:32px;}}
#my-day-list li{{
  display:flex;align-items:flex-start;gap:8px;padding:7px 0;
  border-bottom:1px solid rgba(255,255,255,.08);cursor:grab;
  font-size:.75rem;line-height:1.4;
}}
#my-day-list li:last-child{{border-bottom:none;}}
#my-day-list li.dragging{{opacity:.4;}}
#my-day-list li.drag-over{{border-top:2px solid var(--gold);}}
.day-num{{font-family:'Playfair Display',serif;font-size:.8rem;color:var(--gold);
  flex-shrink:0;width:16px;text-align:right;margin-top:1px;}}
.day-text{{flex:1;}}
.day-client{{font-size:.6rem;letter-spacing:.5px;color:rgba(255,255,255,.35);display:block;margin-top:1px;}}
.day-remove{{font-size:.7rem;color:rgba(255,255,255,.25);cursor:pointer;flex-shrink:0;
  padding:0 2px;line-height:1;transition:color .15s;margin-top:1px;}}
.day-remove:hover{{color:#e07a5a;}}
#my-day-empty{{font-size:.72rem;color:rgba(255,255,255,.3);font-style:italic;padding:4px 0 8px;}}
.day-actions{{display:flex;gap:8px;}}
.day-btn{{flex:1;font-size:.58rem;letter-spacing:1px;text-transform:uppercase;font-weight:600;
  padding:6px 10px;cursor:pointer;border:none;transition:all .15s;}}
#btn-copy{{background:var(--gold);color:var(--ink);}}
#btn-copy:hover{{background:#c8a020;}}
#btn-clear{{background:rgba(255,255,255,.08);color:rgba(255,255,255,.5);}}
#btn-clear:hover{{background:rgba(255,255,255,.15);color:var(--paper);}}
#copy-confirm{{font-size:.6rem;color:var(--gold);text-align:center;margin-top:8px;
  height:14px;opacity:0;transition:opacity .3s;}}
#copy-confirm.show{{opacity:1;}}

@media(max-width:860px){{
  .masthead,.stats-bar,.ai-strip,.main{{padding-left:20px;padding-right:20px;}}
  .task-grid{{grid-template-columns:1fr;}}
  .ai-strip{{flex-direction:column;gap:10px;}}
  .ai-label{{border-right:none;padding-right:0;border-bottom:1px solid var(--border);padding-bottom:10px;}}
  #my-day-panel{{width:calc(100vw - 32px);right:16px;bottom:16px;}}
  #my-day-panel.collapsed{{width:140px;}}
}}
</style>
</head>
<body>
<div class="masthead">
  <div><h1>Daily Briefing</h1><div class="edition">Personal Edition · CBRE Copenhagen</div></div>
  <div class="mast-right">
    <div class="date-line">{today_display}</div>
    <div class="sub">Good morning, Joseph</div>
    <div class="gen-time">Generated {generated_time} · Notion</div>
    <button class="refresh-btn" id="refresh-btn" onclick="triggerRefresh()">↻ Refresh</button>
  </div>
</div>
<div class="stats-bar">
  <div class="stat red"><strong>{len(overdue)}</strong><span>Overdue</span></div>
  <div class="stat gold"><strong>{len(today)}</strong><span>Today</span></div>
  <div class="stat"><strong>{len(waiting)}</strong><span>Waiting On</span></div>
  <div class="stat blue"><strong>{len(this_week)}</strong><span>This Week</span></div>
  <div class="stat"><strong>{total}</strong><span>Total Active</span></div>
</div>
{client_summary_html}
{calendar_strip}
<div class="ai-strip">
  <div class="ai-label">✦ AI<br>Briefing</div>
  <div class="ai-body">{format_summary_html(summary)}</div>
</div>
<div class="main">
  {render_section("Today",      "⚡", today,     today_str, compact=False, colour="#b08a20")}
  {render_section("Overdue",    "🔴", overdue,   today_str, compact=False, colour="#c8502a")}
  {render_section("Waiting On", "⏳", waiting,   today_str, compact=True,  colour="#7a7468")}
  {render_section("This Week",  "📅", this_week, today_str, compact=True,  colour="#2563eb")}
  {render_section("Later",      "📂", later,     today_str, compact=True,  colour="#94a3b8")}
</div>
<div class="footer">Generated by GitHub Actions · {today_display} · {total} active tasks · Powered by Notion</div>

<!-- ── MY DAY PANEL ── -->
<div id="my-day-panel" class="collapsed">
  <div id="my-day-header" onclick="togglePanel()">
    <span id="my-day-title">✦ My Day</span>
    <span id="my-day-count">0</span>
    <span id="my-day-toggle">▲</span>
  </div>
  <div id="my-day-body">
    <ul id="my-day-list"></ul>
    <p id="my-day-empty">Click "+ My Day" on any task to build your list.</p>
    <div class="day-actions">
      <button class="day-btn" id="btn-copy" onclick="copyList()">Copy List</button>
      <button class="day-btn" id="btn-clear" onclick="clearList()">Clear</button>
    </div>
    <div id="copy-confirm">✓ Copied to clipboard</div>
  </div>
</div>

<script>
  var items = [];
  var dragSrc = null;

  function togglePanel() {{
    var p = document.getElementById('my-day-panel');
    var t = document.getElementById('my-day-toggle');
    var collapsed = p.classList.toggle('collapsed');
    t.textContent = collapsed ? '▲' : '▼';
  }}

  function addToMyDay(btn) {{
    if (btn.classList.contains('added')) return;
    var title  = btn.getAttribute('data-title');
    var client = btn.getAttribute('data-client');
    if (items.some(function(i) {{ return i.title === title; }})) return;
    items.push({{ title: title, client: client }});
    btn.classList.add('added');
    btn.textContent = btn.classList.contains('add-day-btn-sm') ? '✓' : '✓ Added';

    // Also mark any duplicate button for same task
    document.querySelectorAll('[data-title="' + title.replace(/"/g, '\\"') + '"]').forEach(function(el) {{
      if (el.classList.contains('add-day-btn') || el.classList.contains('add-day-btn-sm')) {{
        el.classList.add('added');
        el.textContent = el.classList.contains('add-day-btn-sm') ? '✓' : '✓ Added';
      }}
    }});

    renderList();

    // Open panel if collapsed
    var p = document.getElementById('my-day-panel');
    if (p.classList.contains('collapsed')) {{
      p.classList.remove('collapsed');
      document.getElementById('my-day-toggle').textContent = '▼';
    }}
  }}

  function removeItem(idx) {{
    var removed = items.splice(idx, 1)[0];
    // Re-enable the add buttons for this task
    document.querySelectorAll('[data-title="' + removed.title.replace(/"/g, '\\"') + '"]').forEach(function(el) {{
      if (el.classList.contains('add-day-btn') || el.classList.contains('add-day-btn-sm')) {{
        el.classList.remove('added');
        el.textContent = el.classList.contains('add-day-btn-sm') ? '+' : '+ My Day';
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
        '<span class="day-remove" onclick="removeItem(' + idx + ')" title="Remove">✕</span>';

      li.addEventListener('dragstart', function(e) {{
        dragSrc = idx;
        setTimeout(function() {{ li.classList.add('dragging'); }}, 0);
        e.dataTransfer.effectAllowed = 'move';
      }});
      li.addEventListener('dragend', function() {{ li.classList.remove('dragging'); renderList(); }});
      li.addEventListener('dragover', function(e) {{
        e.preventDefault();
        e.dataTransfer.dropEffect = 'move';
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
      btn.textContent = btn.classList.contains('add-day-btn-sm') ? '+' : '+ My Day';
    }});
    renderList();
  }}

  function escHtml(s) {{
    return (s||'').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
  }}

  function toggleCS() {{
    var body = document.getElementById('cs-body');
    var btn  = document.getElementById('cs-btn');
    var open = body.style.display === 'none';
    body.style.display = open ? 'block' : 'none';
    btn.querySelector('span').textContent = (open ? '▾ ' : '▸ ') + btn.querySelector('span').textContent.slice(2);
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
      if (data.ok) {{
        btn.classList.remove('spinning');
        btn.classList.add('done');
        btn.textContent = '✓ Building… reload in ~30s';
        setTimeout(function() {{
          btn.classList.remove('done');
          btn.textContent = '↻ Refresh';
        }}, 35000);
      }} else {{
        btn.classList.remove('spinning');
        btn.classList.add('error');
        btn.textContent = '✗ Failed';
        setTimeout(function() {{
          btn.classList.remove('error');
          btn.textContent = '↻ Refresh';
        }}, 4000);
      }}
    }})
    .catch(function() {{
      btn.classList.remove('spinning');
      btn.classList.add('error');
      btn.textContent = '✗ Failed';
      setTimeout(function() {{
        btn.classList.remove('error');
        btn.textContent = '↻ Refresh';
      }}, 4000);
    }});
  }}

  var WORKER_URL = "{WORKER_URL}";

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
        btn.textContent = 'Error';
        btn.style.color = 'var(--accent)';
      }}
    }})
    .catch(function() {{
      btn.textContent = 'Error';
      btn.style.color = 'var(--accent)';
    }});
  }}
</script>
</body>
</html>"""


# ── MAIN ──────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    today_str = date.today().isoformat()
    print(f"\n{'='*55}")
    print(f"Daily Briefing v2 (Notion) — {today_str}")
    print(f"{'='*55}")

    print("\nBuilding client name map...")
    client_map = build_client_map()

    print("\nFetching active tasks from Notion...")
    tasks = fetch_all_tasks(client_map)

    print("\nBucketing tasks...")
    overdue, today, waiting, this_week, later = bucket_tasks(tasks, today_str)
    print(f"  Today:{len(today)}  Overdue:{len(overdue)}  Waiting:{len(waiting)}  "
          f"This Week:{len(this_week)}  Later:{len(later)}")

    print("\nFetching calendar events...")
    calendar_events = fetch_calendar_events(today_str)

    print("\nGenerating AI summary...")
    summary = generate_summary(overdue, today, waiting, this_week, today_str, calendar_events)
    print(f"  Done ({len(summary)} chars)")

    print("\nBuilding HTML...")
    html = build_html(overdue, today, waiting, this_week, later, summary, today_str, calendar_events)

    with open("index.html", "w", encoding="utf-8") as f:
        f.write(html)

    print(f"  index.html written ({len(html):,} bytes)")
    print(f"{'='*55}\n")
