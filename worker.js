/**
 * worker.js — Daily Dashboard write-back (Google Sheets edition)
 *
 * Deployed as the Cloudflare Worker "notion-proxy". Saves dashboard edits to
 * the Task Database sheet and starts a dashboard rebuild on request.
 *
 *   POST /          {pageId, action: 'update', props: {horizon, due, client, status, priority, notes}}
 *                   {pageId, action: 'done'}
 *                   {pageId, action: 'in_progress'}
 *                   {action: 'bulk_update', items: [{pageId, props}, …]}
 *   POST /refresh   rebuilds the dashboard (GitHub repository_dispatch)
 *   GET  /          health check
 *
 * Every reply is JSON with ok: true|false, as the dashboard expects.
 *
 * Secrets (Cloudflare → Workers → notion-proxy → Settings → Variables and Secrets):
 *   GOOGLE_SA_JSON  the service account's JSON key, pasted whole
 *   GITHUB_TOKEN    GitHub token allowed to trigger the repo's workflow
 *                   (an existing secret under GH_TOKEN or GITHUB_PAT also works)
 */

const SHEET_ID = "1IduH-Kr8TWdOyhDJXxfOrVT6og4q1F1KyGVUO0mHsQ8";
const TAB = "Tasks";
const REPO = "jgilbert82/daily-briefing";
const ALLOWED_ORIGIN = "https://jgilbert82.github.io";

// Dashboard property → sheet column header
const PROP_COLUMNS = {
  horizon: "Horizon",
  due: "Due Date",
  client: "Client",
  status: "Status",
  priority: "Priority",
  notes: "Notes",
};

// Fixed lists. Anything else is refused rather than written into the sheet.
const ALLOWED = {
  horizon: ["", "🔴 Today", "🟡 This Week", "🔵 Next Week", "⚪ Someday"],
  status: ["", "Not Started", "In Progress", "Waiting", "Done"],   // blank reads as Not Started
  priority: ["", "High", "Medium", "Low"],
};

export default {
  async fetch(request, env) {
    if (request.method === "OPTIONS") return new Response(null, { status: 204, headers: cors() });

    const path = new URL(request.url).pathname.replace(/\/+$/, "");
    try {
      if (request.method === "GET") return reply({ ok: true, service: "task-sheet-proxy" });
      if (request.method !== "POST") return reply({ ok: false, error: "Use POST" }, 405);

      if (path === "/refresh") return reply(await triggerRebuild(env));

      const body = await request.json().catch(() => null);
      if (!body || !body.action) return reply({ ok: false, error: "Missing action" }, 400);

      switch (body.action) {
        case "update":
          return reply(await updateOne(env, body.pageId, body.props || {}));
        case "done":
          return reply(await updateOne(env, body.pageId, { status: "Done" }));
        case "in_progress":
          return reply(await updateOne(env, body.pageId, { status: "In Progress" }));
        case "bulk_update":
          return reply(await updateMany(env, Array.isArray(body.items) ? body.items : []));
        default:
          return reply({ ok: false, error: `Unknown action: ${body.action}` }, 400);
      }
    } catch (err) {
      return reply({ ok: false, error: String((err && err.message) || err) }, 500);
    }
  },
};

// ── edits ────────────────────────────────────────────────────────────────────

async function updateOne(env, pageId, props) {
  const res = await updateMany(env, [{ pageId, props }]);
  if (!res.ok) return res;
  return res.updated.length ? { ok: true } : { ok: false, error: `Task not found: ${pageId}` };
}

/** One sheet read and one sheet write, however many tasks change. */
async function updateMany(env, items) {
  if (!items.length) return { ok: false, error: "Nothing to update" };

  // Check every value before touching the sheet
  for (const it of items) {
    if (!it || !it.pageId) return { ok: false, error: "Missing pageId" };
    const bad = invalidProp(it.props || {});
    if (bad) return { ok: false, error: bad };
  }

  const token = await googleToken(env);
  const { header, rowOf } = await readIndex(token);
  const stamp = new Date().toISOString();
  const updatedCol = header.indexOf("Updated");

  const data = [];
  const updated = [];
  const missing = [];
  for (const { pageId, props = {} } of items) {
    const row = rowOf.get(String(pageId));
    if (!row) { missing.push(pageId); continue; }
    for (const [key, value] of Object.entries(props)) {
      if (!(key in PROP_COLUMNS)) continue;
      const col = header.indexOf(PROP_COLUMNS[key]);
      if (col < 0) continue;
      data.push({ range: `'${TAB}'!${colLetter(col + 1)}${row}`, values: [[value == null ? "" : String(value)]] });
    }
    if (updatedCol >= 0) data.push({ range: `'${TAB}'!${colLetter(updatedCol + 1)}${row}`, values: [[stamp]] });
    updated.push(pageId);
  }

  if (data.length) {
    await sheetsApi(token, `values:batchUpdate`, {
      method: "POST",
      body: JSON.stringify({ valueInputOption: "RAW", data }),
    });
  }
  return { ok: true, updated, missing };
}

function invalidProp(props) {
  for (const [key, value] of Object.entries(props)) {
    const v = value == null ? "" : String(value);
    if (ALLOWED[key] && !ALLOWED[key].includes(v)) return `Not an allowed ${key}: ${v}`;
    if (key === "due" && v && !/^\d{4}-\d{2}-\d{2}$/.test(v)) return `Due date must be YYYY-MM-DD: ${v}`;
  }
  return null;
}

/** Header row plus a map of task ID → sheet row number (IDs live in column A). */
async function readIndex(token) {
  const ranges = [`'${TAB}'!1:1`, `'${TAB}'!A:A`].map(r => "ranges=" + encodeURIComponent(r)).join("&");
  const res = await sheetsApi(token, `values:batchGet?${ranges}`, { method: "GET" });
  const header = ((res.valueRanges[0].values || [])[0] || []).map(h => String(h).trim());
  if (header[0] !== "ID") throw new Error("Tasks tab: column A must be headed ID");
  const rowOf = new Map();
  (res.valueRanges[1].values || []).forEach((cells, i) => {
    const id = cells && cells[0] ? String(cells[0]).trim() : "";
    if (i > 0 && id) rowOf.set(id, i + 1);
  });
  return { header, rowOf };
}

async function sheetsApi(token, suffix, init) {
  const r = await fetch(`https://sheets.googleapis.com/v4/spreadsheets/${SHEET_ID}/${suffix}`, {
    ...init,
    headers: { Authorization: `Bearer ${token}`, "Content-Type": "application/json" },
  });
  if (!r.ok) throw new Error(`Sheets API ${r.status}: ${(await r.text()).slice(0, 300)}`);
  return r.json();
}

function colLetter(n) {
  let s = "";
  while (n > 0) {
    const r = (n - 1) % 26;
    s = String.fromCharCode(65 + r) + s;
    n = Math.floor((n - 1) / 26);
  }
  return s;
}

// ── Google service-account sign-in ───────────────────────────────────────────

let cachedToken = null; // { value, expires } — reused while this Worker instance lives

async function googleToken(env) {
  if (cachedToken && cachedToken.expires > Date.now() + 60_000) return cachedToken.value;
  if (!env.GOOGLE_SA_JSON) throw new Error("GOOGLE_SA_JSON secret is not set");

  const sa = JSON.parse(env.GOOGLE_SA_JSON);
  const now = Math.floor(Date.now() / 1000);
  const tokenUri = sa.token_uri || "https://oauth2.googleapis.com/token";
  const enc = obj => b64url(new TextEncoder().encode(JSON.stringify(obj)));
  const unsigned = enc({ alg: "RS256", typ: "JWT" }) + "." + enc({
    iss: sa.client_email,
    scope: "https://www.googleapis.com/auth/spreadsheets",
    aud: tokenUri,
    iat: now,
    exp: now + 3600,
  });

  const key = await crypto.subtle.importKey(
    "pkcs8", pemToDer(sa.private_key),
    { name: "RSASSA-PKCS1-v1_5", hash: "SHA-256" }, false, ["sign"]);
  const sig = await crypto.subtle.sign("RSASSA-PKCS1-v1_5", key, new TextEncoder().encode(unsigned));
  const jwt = unsigned + "." + b64url(new Uint8Array(sig));

  const r = await fetch(tokenUri, {
    method: "POST",
    headers: { "Content-Type": "application/x-www-form-urlencoded" },
    body: "grant_type=" + encodeURIComponent("urn:ietf:params:oauth:grant-type:jwt-bearer") + "&assertion=" + jwt,
  });
  if (!r.ok) throw new Error(`Google sign-in ${r.status}: ${(await r.text()).slice(0, 300)}`);
  const t = await r.json();
  cachedToken = { value: t.access_token, expires: Date.now() + (t.expires_in || 3600) * 1000 };
  return cachedToken.value;
}

function pemToDer(pem) {
  const b64 = pem.replace(/-----[^-]+-----/g, "").replace(/\s+/g, "");
  const bin = atob(b64);
  const out = new Uint8Array(bin.length);
  for (let i = 0; i < bin.length; i++) out[i] = bin.charCodeAt(i);
  return out.buffer;
}

function b64url(bytes) {
  let bin = "";
  for (let i = 0; i < bytes.length; i++) bin += String.fromCharCode(bytes[i]);
  return btoa(bin).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
}

// ── rebuild ──────────────────────────────────────────────────────────────────

async function triggerRebuild(env) {
  const token = env.GITHUB_TOKEN || env.GH_TOKEN || env.GITHUB_PAT;
  if (!token) return { ok: false, error: "GITHUB_TOKEN secret is not set" };
  const r = await fetch(`https://api.github.com/repos/${REPO}/dispatches`, {
    method: "POST",
    headers: {
      Authorization: `Bearer ${token}`,
      Accept: "application/vnd.github+json",
      "User-Agent": "daily-briefing-worker",
      "Content-Type": "application/json",
    },
    body: JSON.stringify({ event_type: "refresh-briefing" }),
  });
  return r.status === 204 ? { ok: true } : { ok: false, error: `GitHub ${r.status}: ${(await r.text()).slice(0, 200)}` };
}

// ── replies ──────────────────────────────────────────────────────────────────

function cors() {
  return {
    "Access-Control-Allow-Origin": ALLOWED_ORIGIN,
    "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
    "Access-Control-Allow-Headers": "Content-Type",
    "Vary": "Origin",
  };
}

function reply(obj, status = 200) {
  return new Response(JSON.stringify(obj), {
    status,
    headers: { "Content-Type": "application/json", ...cors() },
  });
}
