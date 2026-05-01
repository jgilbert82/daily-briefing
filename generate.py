"""
generate.py — fetches tasks from Notion, calls Claude for a summary,
and writes index.html for the Daily Briefing GitHub Pages site.
"""

import os
import re
from datetime import date, datetime
from notion_client import Client
import anthropic

# ── CONFIG ────────────────────────────────────────────────────────────────────

NOTION_TASKS_DB = "b4d5dd97-ad93-40a2-b633-113cfc8b5cb3"

# Map Notion "Google Tasks List" select values to display names
# Tasks with no list set land in "Inbox"
LIST_ORDER = ["NEXT", "THIS WEEK", "WAITING", "MEETING PREP", "LATER", "Inbox"]


# ── NOTION FETCH ──────────────────────────────────────────────────────────────

def fetch_all_tasks():
    notion = Client(auth=os.environ["NOTION_API_KEY"])
    all_tasks = []
    cursor = None

    while True:
        kwargs = {
            "database_id": NOTION_TASKS_DB,
            "filter": {
                "property": "Status",
                "select": {"does_not_equal": "Done"},
            },
            "page_size": 100,
        }
        if cursor:
            kwargs["start_cursor"] = cursor

        response = notion.databases.query(**kwargs)

        for page in response["results"]:
            props = page["properties"]

            # Title
            title_parts = props.get("Task", {}).get("title", [])
            title = "".join(p.get("plain_text", "") for p in title_parts).strip()
            if not title:
                continue  # skip empty rows

            # Status
            status_obj = props.get("Status", {}).get("select") or {}
            status = status_obj.get("name", "")
            if status == "Done":
                continue

            # List (maps to old Google Tasks list)
            list_obj = props.get("Google Tasks List", {}).get("select") or {}
            task_list = list_obj.get("name") or "Inbox"

            # Due date
            due_obj = props.get("Due Date", {}).get("date") or {}
            due_str = (due_obj.get("start") or "")[:10] or None

            # Notes (rich text)
            notes_parts = props.get("Notes", {}).get("rich_text", [])
            note = "".join(p.get("plain_text", "") for p in notes_parts).strip()[:200] or None

            # Client tag
            client_obj = props.get("Client", {}).get("select") or {}
            client_tag = client_obj.get("name") or None

            # Priority
            priority_obj = props.get("Priority", {}).get("select") or {}
            priority = priority_obj.get("name") or None

            all_tasks.append({
                "title":    title,
                "list":     task_list,
                "due":      due_str,
                "note":     note,
                "client":   client_tag,
                "priority": priority,
                "id":       page["id"],
                "url":      page["url"],
            })

        if not response.get("has_more"):
            break
        cursor = response["next_cursor"]

    # Sort: overdue/today first, then by list order, then alphabetical
    def sort_key(t):
        due = t["due"] or "9999-99-99"
        try:
            list_idx = LIST_ORDER.index(t["list"])
        except ValueError:
            list_idx = len(LIST_ORDER)
        return (due, list_idx, t["title"].lower())

    all_tasks.sort(key=sort_key)

    # Print summary
    counts = {}
    for t in all_tasks:
        counts[t["list"]] = counts.get(t["list"], 0) + 1
    for lst in LIST_ORDER:
        if lst in counts:
            print(f"  {lst}: {counts[lst]} tasks")
    for lst, n in counts.items():
        if lst not in LIST_ORDER:
            print(f"  {lst}: {n} tasks")

    return all_tasks


# ── AI SUMMARY ────────────────────────────────────────────────────────────────

def generate_summary(tasks):
    client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
    today = date.today().isoformat()

    if not tasks:
        return "No open tasks found today. Either your lists are clear, or there may be a sync issue — check Notion directly to confirm."

    lines = "\n".join(
        f"[{t['list']}] {t['title']}"
        + (f" (due: {t['due']})" if t["due"] else "")
        + (f" [Client: {t['client']}]" if t["client"] else "")
        for t in tasks
    )

    msg = client.messages.create(
        model="claude-opus-4-5",
        max_tokens=600,
        system=(
            f"You are a sharp, concise executive assistant briefing Joseph, "
            f"a Senior Property Manager at CBRE Copenhagen managing clients including "
            f"AEW, SSCP, Ingka/Hedeland, Mileway, AXA Nordic, Arrow Capital and EQT. "
            f"Today is {today}. "
            f"Write a structured morning briefing with these sections:\n"
            f"1. OVERDUE & URGENT\n"
            f"2. BY CLIENT / CATEGORY\n"
            f"3. WAITING ON\n"
            f"4. TODAY'S PRIORITIES\n\n"
            f"Keep each section to 2-3 sentences max. Plain prose. No markdown bullets."
        ),
        messages=[{"role": "user", "content": f"My open tasks:\n{lines}\n\nMorning briefing please."}],
    )
    return msg.content[0].text.strip()


def extract_priorities(tasks, today_str):
    overdue = [t for t in tasks if t["due"] and t["due"] < today_str]
    today   = [t for t in tasks if t["due"] == today_str]
    nxt     = [t for t in tasks if t["list"] == "NEXT" and not t["due"]]
    picks   = (overdue + today + nxt)[:5]
    labels  = []
    for i, t in enumerate(picks, 1):
        short = t["title"][:55] + ("…" if len(t["title"]) > 55 else "")
        labels.append(f"{i} · {short}")
    return labels


# ── DATE HELPERS ──────────────────────────────────────────────────────────────

def classify_date(due, today_str):
    if not due:            return "no-date"
    if due < today_str:    return "overdue"
    if due == today_str:   return "today"
    return "upcoming"

def fmt_date(due):
    return datetime.strptime(due, "%Y-%m-%d").strftime("%-d %b")

def badge_label(cls, due):
    if cls == "overdue":   return "Overdue"
    if cls == "today":     return "Today"
    if cls == "upcoming":  return fmt_date(due)
    return "—"

def esc(s):
    return (s or "").replace("&","&amp;").replace("<","&lt;").replace(">","&gt;").replace('"',"&quot;")


# ── CLIENT TAG DETECTION ──────────────────────────────────────────────────────

CLIENT_COLOURS = {
    "AEW":      "#2563eb",
    "SSCP":     "#16a34a",
    "Ingka":    "#d97706",
    "Hedeland": "#d97706",
    "Mileway":  "#7c3aed",
    "AXA":      "#0891b2",
    "Arrow":    "#dc2626",
    "EQT":      "#be185d",
    "M&G":      "#065f46",
    "BNPP":     "#1d4ed8",
}

def detect_client(task):
    """Check Notion Client field first, then fall back to keyword scan of title."""
    notion_client = task.get("client") or ""
    for client, colour in CLIENT_COLOURS.items():
        if client.lower() in notion_client.lower():
            return client, colour
    for client, colour in CLIENT_COLOURS.items():
        if client.lower() in task["title"].lower():
            return client, colour
    return None, None

def client_badge(task):
    client, colour = detect_client(task)
    if not client:
        return ""
    return f'<span class="client-badge" style="background:{colour}22;color:{colour};border-color:{colour}88">{client}</span>'

def priority_badge(task):
    p = task.get("priority") or ""
    if p == "High":
        return '<span class="priority-tag high">⬆ High</span>'
    if p == "Low":
        return '<span class="priority-tag low">⬇ Low</span>'
    return ""

def clean_title(title):
    """Strip noisy prefixes."""
    title = re.sub(r'^Day Book[^:]+:\s*', '', title)
    title = re.sub(r'^Inbox\s*:\s*', '', title)
    return title.strip()


# ── RENDER ────────────────────────────────────────────────────────────────────

def render_task(t, today_str):
    cls       = classify_date(t["due"], today_str)
    title     = clean_title(t["title"])
    cbadge    = client_badge(t)
    pbadge    = priority_badge(t)
    note      = (
        f'<div class="task-note">{esc((t["note"] or "")[:120])}'
        f'{"…" if t["note"] and len(t["note"])>120 else ""}</div>'
        if t["note"] else ""
    )
    open_link = f'<a class="open-link" href="{esc(t["url"])}" target="_blank">OPEN IN NOTION ↗</a>'

    return f"""<div class="task">
  <div class="task-top">
    <div class="task-title">{esc(title)}</div>
    {open_link}
  </div>
  <div class="task-meta">
    <span class="badge {cls}">{badge_label(cls, t['due'])}</span>
    <span class="list-tag">{esc(t['list'])}</span>
    {cbadge}{pbadge}
  </div>
  {note}
</div>"""


def render_col(title, tasks, today_str, delay):
    items = "\n".join(render_task(t, today_str) for t in tasks) if tasks else \
        '<div class="empty">Clear.</div>'
    icon = "🔴" if "Priority" in title else ("📅" if "Week" in title else "📂")
    return f"""<div class="col" style="animation-delay:{delay}s">
  <div class="col-head"><span>{icon} {title}</span><span class="col-count">{len(tasks)}</span></div>
  {items}
</div>"""


def format_summary_html(summary_text):
    lines = summary_text.split('\n')
    html_parts = []
    for line in lines:
        line = line.strip()
        if not line:
            continue
        heading_match = re.match(r'^\d+\.\s+([A-Z][A-Z\s/&]+)$', line)
        if heading_match:
            html_parts.append(f'<div class="summary-heading">{esc(heading_match.group(1))}</div>')
        else:
            # Strip any remaining **markdown** bold
            line = re.sub(r'\*\*([^*]+)\*\*', r'\1', line)
            html_parts.append(f'<p>{esc(line)}</p>')
    return '\n'.join(html_parts)


def build_html(tasks, summary, priorities, today_str):
    overdue_count = sum(1 for t in tasks if classify_date(t["due"], today_str) == "overdue")
    today_count   = sum(1 for t in tasks if classify_date(t["due"], today_str) == "today")
    next_count    = sum(1 for t in tasks if t["list"] == "NEXT")
    waiting_count = sum(1 for t in tasks if t["list"] == "WAITING")
    total         = len(tasks)

    col1 = [t for t in tasks if t["list"] == "NEXT" or classify_date(t["due"], today_str) in ("overdue","today")]
    seen = set(id(t) for t in col1)
    col2 = [t for t in tasks if id(t) not in seen and t["list"] == "THIS WEEK"]
    seen.update(id(t) for t in col2)
    col3 = [t for t in tasks if id(t) not in seen]

    priority_chips = "\n".join(f'<span class="priority-chip">{esc(p)}</span>' for p in priorities)
    today_display  = datetime.strptime(today_str, "%Y-%m-%d").strftime("%A %-d %B %Y")
    summary_html   = format_summary_html(summary)
    generated_time = datetime.utcnow().strftime("%H:%M UTC")

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Daily Briefing · {today_display}</title>
<link href="https://fonts.googleapis.com/css2?family=Playfair+Display:ital,wght@0,400;0,700;1,400&family=DM+Sans:wght@300;400;500&display=swap" rel="stylesheet">
<style>
:root {{
  --ink:#1a1a2e; --paper:#f5f0e8; --cream:#ede8dc;
  --accent:#c8502a; --gold:#b08a20; --muted:#7a7468; --border:#d4cfc5;
}}
*{{box-sizing:border-box;margin:0;padding:0;}}
body{{font-family:'DM Sans',sans-serif;background:var(--paper);color:var(--ink);min-height:100vh;}}
.masthead{{background:var(--ink);color:var(--paper);padding:28px 40px 22px;display:flex;align-items:flex-end;justify-content:space-between;flex-wrap:wrap;gap:12px;border-bottom:4px solid var(--accent);}}
.masthead h1{{font-family:'Playfair Display',serif;font-size:2.6rem;letter-spacing:-0.5px;line-height:1;}}
.edition{{font-size:.7rem;letter-spacing:3px;text-transform:uppercase;color:var(--accent);margin-top:7px;font-weight:500;}}
.mast-right{{text-align:right;}}
.date-line{{font-family:'Playfair Display',serif;font-size:1rem;opacity:.85;}}
.sub{{font-size:.68rem;letter-spacing:2px;text-transform:uppercase;opacity:.45;margin-top:5px;}}
.gen-time{{font-size:.6rem;letter-spacing:1px;text-transform:uppercase;color:rgba(255,255,255,.3);margin-top:4px;}}
.stats-bar{{background:var(--ink);display:flex;padding:16px 40px;border-top:1px solid rgba(255,255,255,.08);flex-wrap:wrap;gap:32px;}}
.stat strong{{font-family:'Playfair Display',serif;font-size:1.8rem;display:block;line-height:1;color:var(--paper);}}
.stat span{{font-size:.6rem;letter-spacing:2.5px;text-transform:uppercase;color:rgba(255,255,255,.35);}}
.stat.red strong{{color:#e07a5a;}} .stat.gold strong{{color:#d4aa50;}}
.ai-strip{{background:var(--cream);border-bottom:2px solid var(--border);padding:20px 40px;display:flex;gap:20px;align-items:flex-start;}}
.ai-label{{font-size:.58rem;letter-spacing:3px;text-transform:uppercase;color:var(--gold);font-weight:600;white-space:nowrap;padding-top:3px;flex-shrink:0;}}
.ai-body{{flex:1;}}
.summary-heading{{font-size:.65rem;letter-spacing:2.5px;text-transform:uppercase;color:var(--accent);font-weight:600;margin:12px 0 4px;}}
.summary-heading:first-child{{margin-top:0;}}
.ai-body p{{font-size:.85rem;line-height:1.75;color:var(--ink);font-style:italic;margin-bottom:4px;}}
.ai-priority{{margin-top:12px;display:flex;gap:8px;flex-wrap:wrap;}}
.priority-chip{{font-size:.62rem;letter-spacing:1px;padding:3px 10px;border:1px solid var(--gold);color:var(--gold);background:rgba(176,138,32,.08);font-weight:500;}}
.columns{{display:grid;grid-template-columns:1fr 1fr 1fr;gap:0;padding:0 40px 60px;}}
.col{{padding:28px 24px 0;border-right:1px solid var(--border);animation:fadeUp .4s ease both;}}
.col:first-child{{padding-left:0;}} .col:last-child{{border-right:none;padding-right:0;}}
@keyframes fadeUp{{from{{opacity:0;transform:translateY(8px);}}to{{opacity:1;transform:translateY(0);}}}}
.col-head{{font-size:.6rem;letter-spacing:4px;text-transform:uppercase;color:var(--accent);font-weight:600;border-bottom:2px solid var(--ink);padding-bottom:9px;margin-bottom:18px;display:flex;justify-content:space-between;align-items:baseline;}}
.col-count{{font-size:.75rem;color:var(--muted);letter-spacing:0;font-weight:400;font-family:'Playfair Display',serif;}}
.task{{border-bottom:1px solid var(--border);padding:12px 0;}}
.task:last-child{{border-bottom:none;}}
.task-top{{display:flex;justify-content:space-between;align-items:flex-start;gap:8px;margin-bottom:5px;}}
.task-title{{font-size:.83rem;font-weight:500;line-height:1.45;color:var(--ink);flex:1;}}
.open-link{{font-size:.58rem;letter-spacing:1px;text-transform:uppercase;color:var(--muted);text-decoration:none;white-space:nowrap;border:1px solid var(--border);padding:2px 6px;flex-shrink:0;transition:all .15s;}}
.open-link:hover{{background:var(--ink);color:var(--paper);border-color:var(--ink);}}
.task-meta{{display:flex;gap:6px;flex-wrap:wrap;align-items:center;}}
.badge{{font-size:.56rem;letter-spacing:1.5px;text-transform:uppercase;font-weight:600;padding:2px 7px;border:1px solid;display:inline-block;}}
.badge.overdue{{color:var(--accent);border-color:var(--accent);background:rgba(200,80,42,.07);}}
.badge.today{{color:var(--gold);border-color:var(--gold);background:rgba(176,138,32,.08);}}
.badge.upcoming{{color:var(--muted);border-color:var(--border);}}
.badge.no-date{{color:var(--border);border-color:var(--border);}}
.list-tag{{font-size:.62rem;color:var(--muted);}}
.client-badge{{font-size:.58rem;letter-spacing:1px;text-transform:uppercase;font-weight:600;padding:2px 7px;border:1px solid;display:inline-block;border-radius:2px;}}
.priority-tag{{font-size:.56rem;letter-spacing:1px;text-transform:uppercase;font-weight:600;padding:2px 6px;border-radius:2px;}}
.priority-tag.high{{color:#c8502a;background:rgba(200,80,42,.08);}}
.priority-tag.low{{color:var(--muted);}}
.task-note{{font-size:.71rem;color:var(--muted);margin-top:5px;line-height:1.5;border-left:2px solid var(--border);padding-left:8px;font-style:italic;}}
.empty{{font-size:.8rem;color:var(--muted);font-style:italic;padding:12px 0;}}
.footer{{text-align:center;padding:20px;font-size:.65rem;color:var(--muted);letter-spacing:1px;text-transform:uppercase;border-top:1px solid var(--border);}}
.notion-badge{{display:inline-flex;align-items:center;gap:5px;font-size:.58rem;letter-spacing:1.5px;text-transform:uppercase;color:rgba(255,255,255,.25);margin-top:6px;}}
@media(max-width:860px){{
  .masthead{{padding:20px;}} .stats-bar{{padding:14px 20px;gap:20px;}}
  .ai-strip{{padding:16px 20px;flex-direction:column;gap:8px;}}
  .columns{{grid-template-columns:1fr;padding:0 20px 40px;}}
  .col{{padding:20px 0 0;border-right:none;border-bottom:1px solid var(--border);padding-bottom:20px;}}
  .col:last-child{{border-bottom:none;}}
}}
</style>
</head>
<body>
<div class="masthead">
  <div>
    <h1>Daily Briefing</h1>
    <div class="edition">Personal Edition · CBRE Copenhagen</div>
  </div>
  <div class="mast-right">
    <div class="date-line">{today_display}</div>
    <div class="sub">Good morning, Joseph</div>
    <div class="gen-time">Generated at {generated_time}</div>
    <div class="notion-badge">✦ Powered by Notion</div>
  </div>
</div>

<div class="stats-bar">
  <div class="stat red"><strong>{overdue_count}</strong><span>Overdue</span></div>
  <div class="stat gold"><strong>{today_count}</strong><span>Due Today</span></div>
  <div class="stat"><strong>{next_count}</strong><span>Next Actions</span></div>
  <div class="stat"><strong>{waiting_count}</strong><span>Waiting</span></div>
  <div class="stat"><strong>{total}</strong><span>Total Open</span></div>
</div>

<div class="ai-strip">
  <div class="ai-label">✦ AI<br>Summary</div>
  <div class="ai-body">
    {summary_html}
    <div class="ai-priority">{priority_chips}</div>
  </div>
</div>

<div class="columns">
  {render_col('Priority · Next &amp; Today', col1, today_str, 0.05)}
  {render_col('This Week', col2, today_str, 0.15)}
  {render_col('Waiting · Inbox · Meeting Prep', col3, today_str, 0.25)}
</div>

<div class="footer">Generated by GitHub Actions · {today_display} · {total} open tasks · Powered by Notion</div>
</body>
</html>"""


# ── MAIN ──────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    today_str = date.today().isoformat()
    print(f"\n{'='*50}")
    print(f"Daily Briefing Generator (Notion) — {today_str}")
    print(f"{'='*50}")

    print("\nFetching tasks from Notion...")
    tasks = fetch_all_tasks()
    print(f"\nTotal: {len(tasks)} incomplete tasks fetched.")

    if len(tasks) == 0:
        print("WARNING: No tasks found. Check NOTION_API_KEY and database permissions.")

    print("\nGenerating AI summary...")
    summary = generate_summary(tasks)
    print(f"Summary generated ({len(summary)} chars).")

    priorities = extract_priorities(tasks, today_str)

    print("\nBuilding HTML...")
    html = build_html(tasks, summary, priorities, today_str)

    with open("index.html", "w", encoding="utf-8") as f:
        f.write(html)

    print(f"Done. index.html written ({len(html)} bytes).")
    print(f"{'='*50}\n")
