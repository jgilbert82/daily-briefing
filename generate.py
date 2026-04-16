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

TASK_LISTS = [
    {"id": "R3loUHR2QmRrMlJpMVhaUA",            "name": "NEXT"},
    {"id": "MDkzMjAxNDU3NjI0MDc1NDE3OTM6MDow",  "name": "Inbox"},
    {"id": "YmhRWmFDUmFnY2p4bkxQMQ",            "name": "WAITING"},
    {"id": "dkRESE5rN1FUVmlCRU9ZSg",            "name": "MEETING PREP"},
    {"id": "NDV5NXg4MjZkMC14d0FGQQ",            "name": "THIS WEEK"},
    {"id": "TU5mY3ZnTFY4eWdpaDlGSQ",            "name": "LATER"},
]

GITHUB_REPO = "jgilbert82/daily-briefing"

TAG_LEGEND = """
Task title tags and their meaning:
- [AEWSYD] = AEW / Sydmarken 5
- [AEWKYS] = AEW / Kystvejen 24-30 (Kastrup)
- [AXA]    = AXA Nordic
- [EQT]    = EQT
- [CRM]    = Business development & client relationships
- [FINANCE] = Invoices, billing, Yardi, AR Warsaw
- [ADMIN]  = Internal CBRE / systems / compliance
- [ESG]    = ESG, GRESB, DGNB, BREEAM
- [PERSONAL] = Personal
""".strip()

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
                due_raw  = t.get("due")
                due_str  = due_raw[:10] if due_raw else None
                task_id  = t.get("id", "")
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


def generate_summary(tasks):
    client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
    today  = date.today().isoformat()
    lines  = "\n".join(
        f"[{t['list']}] {t['title']}" + (f" (due: {t['due']})" if t['due'] else "")
        for t in tasks
    )

    prompt = f"""My open tasks for today ({today}):

{lines}

{TAG_LEGEND}

Please summarise my tasks as follows:

1. OVERDUE & URGENT — list any tasks with a due date of today or earlier, individually, with their list name and due date. If none, say "Nothing overdue."

2. BY CLIENT / CATEGORY — group all remaining incomplete tasks by their tag. For each group with active tasks, write 1-3 concise bullet points summarising what needs to happen. Synthesise where multiple tasks relate to the same theme. Skip groups with no active tasks.

3. WAITING ON — summarise items in the WAITING list in one short paragraph.

4. TODAY'S PRIORITIES — a 3-5 sentence narrative of the key priorities for today and this week. Be direct and specific.

Keep the tone concise and professional. Do not repeat task titles verbatim — paraphrase and synthesise. Use plain text with section headers as shown above. No markdown formatting."""

    msg = client.messages.create(
        model="claude-opus-4-5",
        max_tokens=800,
        system=(
            f"You are a sharp, concise executive assistant briefing Joseph, "
            f"a Senior Property Manager at CBRE Copenhagen. Today is {today}. "
            f"You know his portfolio: AEW (Sydmarken 5 and Kystvejen/Kastrup), "
            f"AXA Nordic, EQT, and internal CBRE work. "
            f"Write clearly and professionally. Plain text only. No markdown."
        ),
        messages=[{"role": "user", "content": prompt}],
    )
    return msg.content[0].text.strip()


def extract_priorities(tasks, today_str):
    overdue = [t for t in tasks if t["due"] and t["due"] < today_str]
    today   = [t for t in tasks if t["due"] == today_str]
    nxt     = [t for t in tasks if t["list"] == "NEXT" and not t["due"]]
    picks   = (overdue + today + nxt)[:5]
    return [f"{i+1} · {t['title'][:55]}{'...' if len(t['title'])>55 else ''}" for i, t in enumerate(picks)]


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
    return "-"

def esc(s):
    return (s or "").replace("&","&amp;").replace("<","&lt;").replace(">","&gt;").replace('"',"&quot;")

def render_task(t, today_str):
    cls  = classify_date(t["due"], today_str)
    note = (f'<div class="task-note">{esc((t["note"] or "")[:120])}'
            f'{"..." if t["note"] and len(t["note"])>120 else ""}</div>') if t["note"] else ""
    link = esc(t.get("link","#"))
    return (f'<div class="task" data-status="{cls}" data-list="{esc(t["list"])}">'
            f'<a class="task-title" href="{link}" target="_blank" rel="noopener">'
            f'{esc(t["title"])}<span class="open-icon">&#x2197;</span></a>'
            f'<div class="task-meta">'
            f'<span class="badge {cls}">{badge_label(cls, t["due"])}</span>'
            f'<span class="list-tag">{esc(t["list"])}</span>'
            f'<a class="open-link" href="{link}" target="_blank" rel="noopener">Open in Tasks &#x2197;</a>'
            f'</div>{note}</div>')

def render_col(title, tasks, today_str, delay):
    items = "\n".join(render_task(t, today_str) for t in tasks) if tasks else \
        '<div class="empty-col">Clear.</div>'
    return (f'<div class="col" style="animation-delay:{delay}s">'
            f'<div class="col-head">{title}<span class="col-count">{len(tasks)}</span></div>'
            f'<div class="no-results">No tasks match this filter.</div>'
            f'{items}</div>')


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

    chips         = "\n".join(f'<span class="priority-chip">{esc(p)}</span>' for p in priorities)
    today_display = datetime.strptime(today_str, "%Y-%m-%d").strftime("%A %-d %B %Y")
    generated_at  = datetime.utcnow().strftime("%H:%M UTC")
    actions_url   = f"https://github.com/{GITHUB_REPO}/actions/workflows/daily-briefing.yml"

    # Format summary with section headers as styled HTML blocks
    summary_html = ""
    for line in summary.split("\n"):
        line = line.strip()
        if not line:
            continue
        if line.startswith(("1.", "2.", "3.", "4.")):
            summary_html += f'<div class="ai-section-head">{esc(line)}</div>'
        elif line.startswith("•") or line.startswith("-"):
            summary_html += f'<div class="ai-bullet">{esc(line)}</div>'
        else:
            summary_html += f'<div class="ai-para">{esc(line)}</div>'

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Daily Briefing · {today_display}</title>
<link href="https://fonts.googleapis.com/css2?family=Playfair+Display:ital,wght@0,400;0,700;1,400&family=DM+Sans:wght@300;400;500&display=swap" rel="stylesheet">
<style>
:root{{--ink:#1a1a2e;--paper:#f5f0e8;--cream:#ede8dc;--accent:#c8502a;--gold:#b08a20;--muted:#7a7468;--border:#d4cfc5;}}
*{{box-sizing:border-box;margin:0;padding:0;}}
body{{font-family:'DM Sans',sans-serif;background:var(--paper);color:var(--ink);min-height:100vh;}}
.masthead{{background:var(--ink);color:var(--paper);padding:28px 40px 22px;display:flex;align-items:flex-end;justify-content:space-between;flex-wrap:wrap;gap:12px;border-bottom:4px solid var(--accent);}}
.masthead h1{{font-family:'Playfair Display',serif;font-size:2.6rem;letter-spacing:-0.5px;line-height:1;}}
.edition{{font-size:.7rem;letter-spacing:3px;text-transform:uppercase;color:var(--accent);margin-top:7px;font-weight:500;}}
.masthead-right{{text-align:right;}}
.date-line{{font-family:'Playfair Display',serif;font-size:1rem;opacity:.85;}}
.sub{{font-size:.68rem;letter-spacing:2px;text-transform:uppercase;opacity:.45;margin-top:5px;}}
.last-updated{{font-size:.6rem;letter-spacing:1px;text-transform:uppercase;color:rgba(255,255,255,.3);margin-top:6px;}}
.refresh-btn{{margin-top:10px;font-family:'DM Sans',sans-serif;font-size:.65rem;letter-spacing:2px;text-transform:uppercase;font-weight:500;padding:8px 16px;border:2px solid rgba(255,255,255,.3);background:transparent;color:var(--paper);cursor:pointer;transition:all 0.2s;display:inline-flex;align-items:center;gap:6px;text-decoration:none;}}
.refresh-btn:hover{{border-color:var(--paper);background:rgba(255,255,255,.08);color:var(--paper);}}
.stats-bar{{background:var(--ink);display:flex;padding:16px 40px;border-top:1px solid rgba(255,255,255,.08);flex-wrap:wrap;gap:32px;}}
.stat{{cursor:pointer;padding:6px 12px;border:2px solid transparent;border-radius:2px;transition:border-color 0.15s,background 0.15s;}}
.stat:hover{{border-color:rgba(255,255,255,0.2);}}
.stat.active{{border-color:var(--accent)!important;background:rgba(200,80,42,.15);}}
.stat.active-gold{{border-color:var(--gold)!important;background:rgba(176,138,32,.15);}}
.stat strong{{font-family:'Playfair Display',serif;font-size:1.8rem;display:block;line-height:1;color:var(--paper);}}
.stat span{{font-size:.6rem;letter-spacing:2.5px;text-transform:uppercase;color:rgba(255,255,255,.35);}}
.stat.red strong{{color:#e07a5a;}}.stat.gold strong{{color:#d4aa50;}}
.stat-hint{{font-size:.55rem;color:rgba(255,255,255,.2);letter-spacing:1px;display:block;margin-top:3px;font-style:normal;}}
.filter-bar{{display:none;background:#2a2a3e;padding:10px 40px;align-items:center;gap:12px;border-bottom:2px solid var(--accent);}}
.filter-bar.visible{{display:flex;}}
.filter-label{{font-size:.65rem;letter-spacing:2px;text-transform:uppercase;color:rgba(255,255,255,.5);}}
.filter-active{{font-size:.75rem;color:var(--accent);font-weight:500;letter-spacing:1px;}}
.filter-clear{{font-size:.62rem;letter-spacing:1.5px;text-transform:uppercase;color:rgba(255,255,255,.4);border:1px solid rgba(255,255,255,.2);padding:3px 10px;cursor:pointer;margin-left:auto;background:transparent;font-family:'DM Sans',sans-serif;transition:all 0.15s;}}
.filter-clear:hover{{color:var(--paper);border-color:var(--paper);}}
.ai-strip{{background:var(--cream);border-bottom:2px solid var(--border);padding:20px 40px;display:flex;gap:20px;align-items:flex-start;}}
.ai-label{{font-size:.58rem;letter-spacing:3px;text-transform:uppercase;color:var(--gold);font-weight:600;white-space:nowrap;padding-top:3px;flex-shrink:0;}}
.ai-text{{font-size:.85rem;line-height:1.8;color:var(--ink);}}
.ai-section-head{{font-size:.72rem;letter-spacing:2px;text-transform:uppercase;color:var(--accent);font-weight:600;margin-top:12px;margin-bottom:4px;}}
.ai-section-head:first-child{{margin-top:0;}}
.ai-bullet{{font-size:.83rem;line-height:1.7;color:var(--ink);padding-left:12px;border-left:2px solid var(--border);margin-bottom:3px;}}
.ai-para{{font-size:.83rem;line-height:1.7;color:var(--ink);font-style:italic;margin-bottom:4px;}}
.ai-priority{{margin-top:12px;display:flex;gap:8px;flex-wrap:wrap;}}
.priority-chip{{font-size:.62rem;letter-spacing:1px;padding:3px 10px;border:1px solid var(--gold);color:var(--gold);background:rgba(176,138,32,.08);font-weight:500;}}
.columns{{display:grid;grid-template-columns:1fr 1fr 1fr;gap:0;padding:0 40px 60px;}}
.col{{padding:28px 24px 0;border-right:1px solid var(--border);animation:fadeUp .4s ease both;}}
.col:first-child{{padding-left:0;}}.col:last-child{{border-right:none;padding-right:0;}}
@keyframes fadeUp{{from{{opacity:0;transform:translateY(8px);}}to{{opacity:1;transform:translateY(0);}}}}
.col-head{{font-size:.6rem;letter-spacing:4px;text-transform:uppercase;color:var(--accent);font-weight:600;border-bottom:2px solid var(--ink);padding-bottom:9px;margin-bottom:18px;display:flex;justify-content:space-between;align-items:baseline;}}
.col-count{{font-size:.75rem;color:var(--muted);letter-spacing:0;font-weight:400;font-family:'Playfair Display',serif;}}
.task{{border-bottom:1px solid var(--border);padding:11px 0;}}
.task:last-child{{border-bottom:none;}}
.task.hidden{{display:none;}}
a.task-title{{font-size:.83rem;font-weight:500;line-height:1.45;color:var(--ink);display:block;margin-bottom:5px;text-decoration:underline;text-decoration-color:var(--border);text-underline-offset:3px;transition:color 0.15s,text-decoration-color 0.15s;}}
a.task-title:hover{{color:var(--accent);text-decoration-color:var(--accent);}}
.open-icon{{font-size:.7rem;color:var(--accent);margin-left:4px;opacity:0.6;}}
.task-meta{{display:flex;gap:8px;flex-wrap:wrap;align-items:center;}}
.badge{{font-size:.56rem;letter-spacing:1.5px;text-transform:uppercase;font-weight:600;padding:2px 7px;border:1px solid;display:inline-block;}}
.badge.overdue{{color:var(--accent);border-color:var(--accent);background:rgba(200,80,42,.07);}}
.badge.today{{color:var(--gold);border-color:var(--gold);background:rgba(176,138,32,.08);}}
.badge.upcoming{{color:var(--muted);border-color:var(--border);}}
.badge.no-date{{color:var(--border);border-color:var(--border);}}
.list-tag{{font-size:.62rem;color:var(--muted);}}
.open-link{{font-size:.6rem;letter-spacing:1px;text-transform:uppercase;color:var(--accent);text-decoration:none;font-weight:500;border:1px solid var(--accent);padding:1px 7px;opacity:0.7;transition:opacity 0.15s,background 0.15s;margin-left:auto;}}
.open-link:hover{{opacity:1;background:rgba(200,80,42,.07);}}
.task-note{{font-size:.71rem;color:var(--muted);margin-top:5px;line-height:1.5;border-left:2px solid var(--border);padding-left:8px;font-style:italic;}}
.empty-col{{font-size:.8rem;color:var(--muted);font-style:italic;padding:12px 0;}}
.no-results{{font-size:.8rem;color:var(--muted);font-style:italic;padding:12px 0;display:none;}}
.footer{{text-align:center;padding:20px;font-size:.65rem;color:var(--muted);letter-spacing:1px;text-transform:uppercase;border-top:1px solid var(--border);}}
@media(max-width:860px){{
  .masthead{{padding:20px;}}.stats-bar{{padding:14px 20px;gap:16px;}}
  .ai-strip{{padding:16px 20px;flex-direction:column;gap:8px;}}
  .filter-bar{{padding:10px 20px;}}
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
  <div class="masthead-right">
    <div class="date-line">{today_display}</div>
    <div class="sub">Good morning, Joseph</div>
    <div class="last-updated">Generated at {generated_at}</div>
    <a class="refresh-btn" href="{actions_url}" target="_blank" rel="noopener">
      &#x21BA; Refresh Briefing
    </a>
  </div>
</div>

<div class="stats-bar">
  <div class="stat red" id="stat-overdue" onclick="applyFilter('overdue')">
    <strong>{overdue_count}</strong><span>Overdue</span>
    <em class="stat-hint">click to filter</em>
  </div>
  <div class="stat gold" id="stat-today" onclick="applyFilter('today')">
    <strong>{today_count}</strong><span>Due Today</span>
    <em class="stat-hint">click to filter</em>
  </div>
  <div class="stat" id="stat-next" onclick="applyFilter('next')">
    <strong>{next_count}</strong><span>Next Actions</span>
    <em class="stat-hint">click to filter</em>
  </div>
  <div class="stat" id="stat-waiting" onclick="applyFilter('waiting')">
    <strong>{waiting_count}</strong><span>Waiting</span>
    <em class="stat-hint">click to filter</em>
  </div>
  <div class="stat" id="stat-all">
    <strong>{total}</strong><span>Total Open</span>
  </div>
</div>

<div class="filter-bar" id="filterBar">
  <span class="filter-label">Showing:</span>
  <span class="filter-active" id="filterLabel"></span>
  <button class="filter-clear" onclick="clearFilter()">&#x2715; Show All</button>
</div>

<div class="ai-strip">
  <div class="ai-label">&#x2726; AI<br>Summary</div>
  <div style="flex:1">
    <div class="ai-text">{summary_html}</div>
    <div class="ai-priority">{chips}</div>
  </div>
</div>

<div class="columns">
  {render_col('&#x1F534; Priority · Next &amp; Today', col1, today_str, 0.05)}
  {render_col('&#x1F4C5; This Week', col2, today_str, 0.15)}
  {render_col('&#x1F4C2; Waiting · Inbox · Meeting Prep', col3, today_str, 0.25)}
</div>

<div class="footer">Generated by GitHub Actions · {today_display} · {total} open tasks · Click any task to open in Google Tasks</div>

<script>
let activeFilter = null;
const FILTER_LABELS = {{
  overdue: '&#x1F534; Overdue tasks only',
  today:   '&#x1F7E1; Due today only',
  next:    'Next Actions only',
  waiting: 'Waiting tasks only',
}};

function applyFilter(filter) {{
  if (activeFilter === filter) {{ clearFilter(); return; }}
  activeFilter = filter;
  document.querySelectorAll('.stat').forEach(s => s.classList.remove('active','active-gold'));
  const el = document.getElementById('stat-' + filter);
  if (el) el.classList.add(filter === 'today' ? 'active-gold' : 'active');
  document.getElementById('filterBar').classList.add('visible');
  document.getElementById('filterLabel').innerHTML = FILTER_LABELS[filter] || filter;
  document.querySelectorAll('.task').forEach(task => {{
    const show = (
      (filter === 'overdue'  && task.dataset.status === 'overdue') ||
      (filter === 'today'    && task.dataset.status === 'today')   ||
      (filter === 'next'     && task.dataset.list   === 'NEXT')    ||
      (filter === 'waiting'  && task.dataset.list   === 'WAITING')
    );
    task.classList.toggle('hidden', !show);
  }});
}}

function clearFilter() {{
  activeFilter = null;
  document.querySelectorAll('.stat').forEach(s => s.classList.remove('active','active-gold'));
  document.getElementById('filterBar').classList.remove('visible');
  document.querySelectorAll('.task').forEach(t => t.classList.remove('hidden'));
}}
</script>
</body>
</html>"""


if __name__ == "__main__":
    today_str = date.today().isoformat()
    print(f"Fetching tasks for {today_str}...")
    tasks = fetch_all_tasks()
    print(f"  Fetched {len(tasks)} tasks across {len(TASK_LISTS)} lists.")
    print("Generating AI summary...")
    summary = generate_summary(tasks)
    print(f"  Summary: {summary[:80]}...")
    priorities = extract_priorities(tasks, today_str)
    print("Building HTML...")
    html = build_html(tasks, summary, priorities, today_str)
    with open("index.html", "w", encoding="utf-8") as f:
        f.write(html)
    print("Done. index.html written.")
