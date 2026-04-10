"""
generate.py — fetches Google Tasks, calls Claude for a summary,
and writes index.html for the Daily Briefing GitHub Pages site.
"""

import os
import json
from datetime import date, datetime
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
import anthropic

# ── CONFIG ────────────────────────────────────────────────────────────────────

TASK_LISTS = [
    {"id": "R3loUHR2QmRrMlJpMVhaUA",            "name": "NEXT"},
    {"id": "MDkzMjAxNDU3NjI0MDc1NDE3OTM6MDow",  "name": "Inbox"},
    {"id": "YmhRWmFDUmFnY2p4bkxQMQ",            "name": "WAITING"},
    {"id": "dkRESE5rN1FUVmlCRU9ZSg",            "name": "MEETING PREP"},
    {"id": "NDV5NXg4MjZkMC14d0FGQQ",            "name": "THIS WEEK"},
    {"id": "TU5mY3ZnTFY4eWdpaDlGSQ",            "name": "LATER"},
]

# ── GOOGLE TASKS ──────────────────────────────────────────────────────────────

def get_tasks_service():
    creds_data = json.loads(os.environ["GOOGLE_CREDENTIALS_JSON"])
    creds = Credentials(
        token=creds_data["token"],
        refresh_token=creds_data["refresh_token"],
        token_uri="https://oauth2.googleapis.com/token",
        client_id=creds_data["client_id"],
        client_secret=creds_data["client_secret"],
        scopes=["https://www.googleapis.com/auth/tasks.readonly"],
    )
    return build("tasks", "v1", credentials=creds)


def fetch_all_tasks():
    service = get_tasks_service()
    all_tasks = []
    for lst in TASK_LISTS:
        try:
            result = service.tasks().list(
                tasklist=lst["id"],
                showCompleted=False,
                showHidden=False,
                maxResults=100,
            ).execute()
            for t in result.get("items", []):
                if t.get("status") == "completed":
                    continue
                due_raw = t.get("due")
                due_str = due_raw[:10] if due_raw else None
                task_id = t.get("id", "")
                web_link = f"https://tasks.google.com/task/{task_id}?sa=6"
                all_tasks.append({
                    "title": t.get("title", "").strip(),
                    "list":  lst["name"],
                    "due":   due_str,
                    "note":  (t.get("notes") or "").strip()[:200] or None,
                    "link":  web_link,
                })
        except Exception as e:
            print(f"Warning: could not fetch {lst['name']}: {e}")
    return all_tasks


# ── AI SUMMARY ────────────────────────────────────────────────────────────────

def generate_summary(tasks):
    client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
    today = date.today().isoformat()
    lines = "\n".join(
        f"[{t['list']}] {t['title']}" + (f" (due: {t['due']})" if t['due'] else "")
        for t in tasks
    )
    msg = client.messages.create(
        model="claude-opus-4-5",
        max_tokens=500,
        system=(
            f"You are a sharp, concise executive assistant briefing Joseph, "
            f"a Senior Property Manager at CBRE Copenhagen. Today is {today}. "
            f"Write a 3–5 sentence morning briefing: highlight the most urgent/overdue items, "
            f"what to prioritise today, and one clear suggestion. "
            f"Plain prose only. No bullets. No markdown."
        ),
        messages=[{"role": "user", "content": f"My open tasks:\n{lines}\n\nBriefing please."}],
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


# ── HTML HELPERS ──────────────────────────────────────────────────────────────

def classify_date(due, today_str):
    if not due:          return "no-date"
    if due < today_str:  return "overdue"
    if due == today_str: return "today"
    return "upcoming"

def fmt_date(due):
    return datetime.strptime(due, "%Y-%m-%d").strftime("%-d %b")

def badge_label(cls, due):
    if cls == "overdue":  return "Overdue"
    if cls == "today":    return "Today"
    if cls == "upcoming": return fmt_date(due)
    return "—"

def esc(s):
    return (s or "").replace("&","&amp;").replace("<","&lt;").replace(">","&gt;").replace('"',"&quot;")

def render_task(t, today_str):
    cls  = classify_date(t["due"], today_str)
    note = f'<div class="task-note">{esc((t["note"] or "")[:120])}{"…" if t["note"] and len(t["note"]) > 120 else ""}</div>' if t["note"] else ""
    link = esc(t.get("link", "#"))
    return f"""<div class="task">
  <a class="task-title" href="{link}" target="_blank" rel="noopener">{esc(t['title'])}<span class="open-icon">↗</span></a>
  <div class="task-meta">
    <span class="badge {cls}">{badge_label(cls, t['due'])}</span>
    <span class="list-tag">{esc(t['list'])}</span>
    <a class="open-link" href="{link}" target="_blank" rel="noopener">Open in Tasks ↗</a>
  </div>
  {note}
</div>"""

def render_col(title, tasks, today_str, delay):
    items = "\n".join(render_task(t, today_str) for t in tasks) if tasks else \
        '<div style="font-size:0.8rem;color:var(--muted);font-style:italic;padding:12px 0">Clear.</div>'
    return f"""<div class="col" style="animation-delay:{delay}s">
  <div class="col-head">{title}<span class="col-count">{len(tasks)}</span></div>
  {items}
</div>"""


# ── HTML BUILD ────────────────────────────────────────────────────────────────

def build_html(tasks, summary, priorities, today_str):
    overdue_count = sum(1 for t in tasks if classify_date(t["due"], today_str) == "overdue")
    today_count   = sum(1 for t in tasks if classify_date(t["due"], today_str) == "today")
    next_count    = sum(1 for t in tasks if t["list"] == "NEXT")
    waiting_count = sum(1 for t in tasks if t["list"] == "WAITING")
    total         = len(tasks)

    col1_tasks = [t for t in tasks if t["list"] == "NEXT" or classify_date(t["due"], today_str) in ("overdue", "today")]
    seen = set(id(t) for t in col1_tasks)
    col2_tasks = [t for t in tasks if id(t) not in seen and t["list"] == "THIS WEEK"]
    seen.update(id(t) for t in col2_tasks)
    col3_tasks = [t for t in tasks if id(t) not in seen]

    priority_chips = "\n".join(f'<span class="priority-chip">{esc(p)}</span>' for p in priorities)
    today_display  = datetime.strptime(today_str, "%Y-%m-%d").strftime("%A %-d %B %Y")

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Daily Briefing · {today_display}</title>
<link href="https://fonts.googleapis.com/css2?family=Playfair+Display:ital,wght@0,400;0,700;1,400&family=DM+Sans:wght@300;400;500&display=swap" rel="stylesheet">
<style>
:root {{
  --ink: #1a1a2e; --paper: #f5f0e8; --cream: #ede8dc;
  --accent: #c8502a; --gold: #b08a20; --muted: #7a7468; --border: #d4cfc5;
}}
* {{ box-sizing: border-box; margin: 0; padding: 0; }}
body {{ font-family: 'DM Sans', sans-serif; background: var(--paper); color: var(--ink); min-height: 100vh; }}
.masthead {{ background: var(--ink); color: var(--paper); padding: 28px 40px 22px; display: flex; align-items: flex-end; justify-content: space-between; flex-wrap: wrap; gap: 12px; border-bottom: 4px solid var(--accent); }}
.masthead h1 {{ font-family: 'Playfair Display', serif; font-size: 2.6rem; letter-spacing: -0.5px; line-height: 1; }}
.edition {{ font-size: .7rem; letter-spacing: 3px; text-transform: uppercase; color: var(--accent); margin-top: 7px; font-weight: 500; }}
.date-line {{ font-family: 'Playfair Display', serif; font-size: 1rem; opacity: .85; text-align: right; }}
.sub {{ font-size: .68rem; letter-spacing: 2px; text-transform: uppercase; opacity: .45; margin-top: 5px; text-align: right; }}
.last-updated {{ font-size: .6rem; letter-spacing: 1px; text-transform: uppercase; color: rgba(255,255,255,.3); margin-top: 6px; text-align: right; }}
.stats-bar {{ background: var(--ink); display: flex; padding: 16px 40px; border-top: 1px solid rgba(255,255,255,.08); flex-wrap: wrap; gap: 32px; }}
.stat strong {{ font-family: 'Playfair Display', serif; font-size: 1.8rem; display: block; line-height: 1; color: var(--paper); }}
.stat span {{ font-size: .6rem; letter-spacing: 2.5px; text-transform: uppercase; color: rgba(255,255,255,.35); }}
.stat.red strong {{ color: #e07a5a; }} .stat.gold strong {{ color: #d4aa50; }}
.ai-strip {{ background: var(--cream); border-bottom: 2px solid var(--border); padding: 20px 40px; display: flex; gap: 20px; align-items: flex-start; }}
.ai-label {{ font-size: .58rem; letter-spacing: 3px; text-transform: uppercase; color: var(--gold); font-weight: 600; white-space: nowrap; padding-top: 3px; flex-shrink: 0; }}
.ai-text {{ font-size: .85rem; line-height: 1.8; color: var(--ink); font-style: italic; }}
.ai-priority {{ margin-top: 10px; display: flex; gap: 8px; flex-wrap: wrap; font-style: normal; }}
.priority-chip {{ font-size: .62rem; letter-spacing: 1px; padding: 3px 10px; border: 1px solid var(--gold); color: var(--gold); background: rgba(176,138,32,.08); font-weight: 500; }}
.columns {{ display: grid; grid-template-columns: 1fr 1fr 1fr; gap: 0; padding: 0 40px 60px; }}
.col {{ padding: 28px 24px 0; border-right: 1px solid var(--border); animation: fadeUp .4s ease both; }}
.col:first-child {{ padding-left: 0; }} .col:last-child {{ border-right: none; padding-right: 0; }}
@keyframes fadeUp {{ from {{ opacity: 0; transform: translateY(8px); }} to {{ opacity: 1; transform: translateY(0); }} }}
.col-head {{ font-size: .6rem; letter-spacing: 4px; text-transform: uppercase; color: var(--accent); font-weight: 600; border-bottom: 2px solid var(--ink); padding-bottom: 9px; margin-bottom: 18px; display: flex; justify-content: space-between; align-items: baseline; }}
.col-count {{ font-size: .75rem; color: var(--muted); letter-spacing: 0; font-weight: 400; font-family: 'Playfair Display', serif; }}
.task {{ border-bottom: 1px solid var(--border); padding: 11px 0; }}
.task:last-child {{ border-bottom: none; }}
a.task-title {{
  font-size: .83rem; font-weight: 500; line-height: 1.45;
  color: var(--ink); display: block; margin-bottom: 5px;
  text-decoration: underline; text-decoration-color: var(--border);
  text-underline-offset: 3px; transition: color 0.15s, text-decoration-color 0.15s;
}}
a.task-title:hover {{ color: var(--accent); text-decoration-color: var(--accent); }}
.open-icon {{ font-size: .7rem; color: var(--accent); margin-left: 4px; opacity: 0.6; }}
.task-meta {{ display: flex; gap: 8px; flex-wrap: wrap; align-items: center; }}
.badge {{ font-size: .56rem; letter-spacing: 1.5px; text-transform: uppercase; font-weight: 600; padding: 2px 7px; border: 1px solid; display: inline-block; }}
.badge.overdue {{ color: var(--accent); border-color: var(--accent); background: rgba(200,80,42,.07); }}
.badge.today {{ color: var(--gold); border-color: var(--gold); background: rgba(176,138,32,.08); }}
.badge.upcoming {{ color: var(--muted); border-color: var(--border); }}
.badge.no-date {{ color: var(--border); border-color: var(--border); }}
.list-tag {{ font-size: .62rem; color: var(--muted); }}
.open-link {{
  font-size: .6rem; letter-spacing: 1px; text-transform: uppercase;
  color: var(--accent); text-decoration: none; font-weight: 500;
  border: 1px solid var(--accent); padding: 1px 7px;
  opacity: 0.7; transition: opacity 0.15s, background 0.15s;
  margin-left: auto;
}}
.open-link:hover {{ opacity: 1; background: rgba(200,80,42,.07); }}
.task-note {{ font-size: .71rem; color: var(--muted); margin-top: 5px; line-height: 1.5; border-left: 2px solid var(--border); padding-left: 8px; font-style: italic; }}
.footer {{ text-align: center; padding: 20px; font-size: .65rem; color: var(--muted); letter-spacing: 1px; text-transform: uppercase; border-top: 1px solid var(--border); }}
@media (max-width: 860px) {{
  .masthead {{ padding: 20px; }} .stats-bar {{ padding: 14px 20px; gap: 20px; }}
  .ai-strip {{ padding: 16px 20px; flex-direction: column; gap: 8px; }}
  .columns {{ grid-template-columns: 1fr; padding: 0 20px 40px; }}
  .col {{ padding: 20px 0 0; border-right: none; border-bottom: 1px solid var(--border); padding-bottom: 20px; }}
  .col:last-child {{ border-bottom: none; }}
}}
</style>
</head>
<body>
<div class="masthead">
  <div>
    <h1>Daily Briefing</h1>
    <div class="edition">Personal Edition · CBRE Copenhagen</div>
  </div>
  <div>
    <div class="date-line">{today_display}</div>
    <div class="sub">Good morning, Joseph</div>
    <div class="last-updated">Auto-generated at 06:00</div>
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
  <div>
    <div class="ai-text">{esc(summary)}</div>
    <div class="ai-priority">{priority_chips}</div>
  </div>
</div>
<div class="columns">
  {render_col('🔴 Priority · Next &amp; Today', col1_tasks, today_str, 0.05)}
  {render_col('📅 This Week', col2_tasks, today_str, 0.15)}
  {render_col('📂 Waiting · Inbox · Meeting Prep', col3_tasks, today_str, 0.25)}
</div>
<div class="footer">Generated by GitHub Actions · {today_display} · {total} open tasks</div>
</body>
</html>"""


# ── MAIN ──────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    today_str = date.today().isoformat()
    print(f"Fetching tasks for {today_str}…")
    tasks = fetch_all_tasks()
    print(f"  Fetched {len(tasks)} tasks across {len(TASK_LISTS)} lists.")
    print("Generating AI summary…")
    summary = generate_summary(tasks)
    print(f"  Summary: {summary[:80]}…")
    priorities = extract_priorities(tasks, today_str)
    print("Building HTML…")
    html = build_html(tasks, summary, priorities, today_str)
    with open("index.html", "w", encoding="utf-8") as f:
        f.write(html)
    print("Done. index.html written.")
