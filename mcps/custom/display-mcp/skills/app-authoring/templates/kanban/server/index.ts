// A kanban board that members from contributor up change and everyone the
// app admits reads, a link included: Bun + bun:sqlite, the reference server of
// the OtoDock app runtime. The platform runs this file on Bun inside the
// app's own sandbox; the only writable place is $OTODOCK_DATA_DIR
// (/app/data), the only way in is the proxy (X-OtoDock-Viewer names who is
// calling). Listen on 0.0.0.0:$PORT and answer GET /_health.
import { Database } from "bun:sqlite";

const port = Number(process.env.PORT || 3000);
const dataDir = process.env.OTODOCK_DATA_DIR || "/app/data";
const appId = process.env.OTODOCK_APP_ID || "";
const db = new Database(`${dataDir}/app.db`, { create: true });
db.exec("PRAGMA journal_mode = WAL");
// Additive migrations only: CREATE TABLE IF NOT EXISTS and ADD COLUMN. The
// previous release keeps running against the same file during a deploy.
db.exec(`CREATE TABLE IF NOT EXISTS cards (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  title TEXT NOT NULL,
  col TEXT NOT NULL DEFAULT 'todo',
  position INTEGER NOT NULL DEFAULT 0,
  moved_by TEXT NOT NULL DEFAULT '',
  updated_at TEXT NOT NULL
)`);

// A handler fire is retried with the SAME delivery id, so remember the
// ones already done (the app-authoring skill, "Handlers").
db.exec(`CREATE TABLE IF NOT EXISTS handled (id TEXT PRIMARY KEY, at TEXT NOT NULL)`);

const COLUMNS = ["todo", "doing", "done"];

// ── who is calling ─────────────────────────────────────────────────────────
// The proxy signs every claim with this app's own Ed25519 key and hands the
// public half in OTODOCK_APP_PUBLIC_KEY. Only the proxy reaches this port,
// so decoding alone would do. Verifying keeps the rule true wherever this
// code is copied (the app-authoring skill, "Verify the claim").
type Claim = {
  principal?: string; sub?: string; username?: string; role?: string; external?: boolean;
  handler?: string; delivery?: string; caller_slug?: string; purpose?: string; aud?: string; exp?: number;
};
const b64url = (s: string) => Buffer.from(s.replace(/-/g, "+").replace(/_/g, "/"), "base64");
let appKey: Promise<CryptoKey | null> | null = null;
function publicKey(): Promise<CryptoKey | null> {
  appKey ??= (async () => {
    const raw = process.env.OTODOCK_APP_PUBLIC_KEY || "";
    if (!raw) return null;
    try {
      return await crypto.subtle.importKey("raw", b64url(raw), { name: "Ed25519" }, false, ["verify"]);
    } catch {
      return null;
    }
  })();
  return appKey;
}
// A viewer and a link visitor carry purpose app_viewer. The agent's own
// sessions, placed agents, handler wakes and another app over a binding
// (its claim rides X-OtoDock-Caller) carry app_caller.
async function claimOf(req: Request, header = "x-otodock-viewer"): Promise<Claim | null> {
  const [head, body, sig] = (req.headers.get(header) || "").split(".");
  const key = await publicKey();
  if (!head || !body || !sig || !key) return null;
  try {
    const ok = await crypto.subtle.verify("Ed25519", key, b64url(sig), new TextEncoder().encode(`${head}.${body}`));
    if (!ok) return null;
    const c = JSON.parse(b64url(body).toString("utf8")) as Claim;
    if (c.aud !== `app:${appId}`) return null;
    if (c.purpose !== "app_viewer" && c.purpose !== "app_caller") return null;
    if (!c.exp || c.exp * 1000 < Date.now()) return null;
    return c;
  } catch {
    return null;
  }
}

// Roles by rank. Any other word (agent, app, none) ranks lowest, below viewer.
const RANK = new Map([["viewer", 1], ["contributor", 2], ["editor", 3], ["manager", 4], ["admin", 5]]);
const rankOf = (role?: string) => RANK.get(String(role ?? "")) ?? 0;

// Who may change the board: a person at contributor or above, or this
// agent's own session with no person behind it. A link never writes here.
function canWrite(c: Claim | null, basis: string): boolean {
  if (!c || c.external) return false;
  if (basis === "agent" && c.principal === "agent" && c.role === "agent") return true;
  return rankOf(c.role) >= rankOf("contributor");
}

// The name a card shows as "Moved by". An agent session names its agent
// and, when a person drives it, that person too, so a card the agent added
// is never mistaken for one the person dragged.
function nameOf(c: Claim | null, basis: string): string {
  if (basis === "binding") return c?.caller_slug ? `the ${c.caller_slug} app` : "another app";
  if (!c) return basis === "agent" ? "the agent" : "someone";
  if (c.principal === "agent" || basis === "agent") return (c.username ? c.username + " via " : "") + "the agent";
  if (c.principal === "placement") return (c.username ? c.username + " via " : "") + "another agent";
  if (c.principal === "platform" || basis === "platform") return "the schedule";
  return c.username || (c.external ? "a guest" : "someone");
}

// A JSON body as a plain object. No body, bad JSON, null or an array is an
// empty object, so a field check answers it in words.
async function readBody(req: Request): Promise<Record<string, unknown>> {
  const body = await req.json().catch(() => null);
  return body && typeof body === "object" && !Array.isArray(body) ? (body as Record<string, unknown>) : {};
}

// ── the board ──────────────────────────────────────────────────────────────
function cards() {
  return db.query("SELECT id, title, col, position, moved_by, updated_at FROM cards ORDER BY col, position, id").all();
}
function summary() {
  return db.query("SELECT id, title, col, moved_by FROM cards ORDER BY col, position, id").all();
}
function moveCard(id: number, col: unknown, position: unknown, who: string): Response {
  if (!COLUMNS.includes(String(col))) return json({ error: "The column must be todo, doing or done." }, 400);
  const last = (db.query("SELECT COALESCE(MAX(position), 0) AS p FROM cards WHERE col = ?").get(String(col)) as any).p;
  const asked = Number(position);
  const pos = Number.isFinite(asked) ? Math.max(0, Math.min(Math.trunc(asked), last + 1)) : last + 1;
  const changed = db.run("UPDATE cards SET col = ?, position = ?, moved_by = ?, updated_at = ? WHERE id = ?",
    [String(col), pos, who, new Date().toISOString(), id]);
  if (!changed.changes) return json({ error: "That card is gone. Someone removed it." }, 404);
  broadcast();
  return json({ ok: true });
}

const sockets = new Set<any>();
function broadcast() {
  const msg = JSON.stringify({ type: "cards", cards: cards() });
  for (const ws of sockets) {
    try { ws.send(msg); } catch { sockets.delete(ws); }
  }
  publish();
}

// What other apps may take (the app-authoring skill, "Bindings"): the board
// as a snapshot the platform serves from this file without waking us
// (written to a temporary name and renamed, so a reader never sees half a
// file) and one event per change, which wakes the apps that bound to us and
// listen. Both are declared in app.json under "exports". Best effort, never
// fatal.
let publishTimer: ReturnType<typeof setTimeout> | null = null;
function publish() {
  if (publishTimer) return;
  publishTimer = setTimeout(async () => {
    publishTimer = null;
    try {
      const { mkdirSync, renameSync } = await import("node:fs");
      mkdirSync(`${dataDir}/exports`, { recursive: true });
      const body = JSON.stringify({ cards: cards(), written_at: new Date().toISOString() });
      await Bun.write(`${dataDir}/exports/board.json.tmp`, body);
      renameSync(`${dataDir}/exports/board.json.tmp`, `${dataDir}/exports/board.json`);
    } catch (e) { console.log("snapshot not written: " + String(e).slice(0, 120)); }
    const proxy = process.env.OTODOCK_PROXY_URL, token = process.env.OTODOCK_APP_TOKEN;
    if (!proxy || !appId || !token) return;
    try {
      await fetch(`${proxy}/v1/apps/${appId}/events`, {
        method: "POST", signal: AbortSignal.timeout(3000),
        headers: { authorization: `Bearer ${token}`, "content-type": "application/json" },
        body: JSON.stringify({ name: "card-moved", payload: { cards: cards().length } }),
      });
    } catch (e) { console.log("event not emitted: " + String(e).slice(0, 120)); }
  }, 300);
}

const json = (body: unknown, status = 200) =>
  new Response(JSON.stringify(body), { status, headers: { "content-type": "application/json" } });

console.log(`kanban listening on ${port}`);
Bun.serve({
  port,
  hostname: "0.0.0.0",
  async fetch(req, server) {
    const url = new URL(req.url);
    if (url.pathname === "/_health") return new Response("ok");
    if (url.pathname === "/live") {
      if (server.upgrade(req)) return undefined as unknown as Response;
      return new Response("Open this page from the app.", { status: 400 });
    }
    // The platform wakes a declared handler here, and nothing else can:
    // /_handler/* is refused for every caller but the proxy. Check that the
    // claim names THIS handler and THIS delivery, drop a repeat, do the
    // work, answer within the minute (the app-authoring skill, "Handlers").
    if (url.pathname.startsWith("/_handler/")) {
      const name = url.pathname.slice("/_handler/".length);
      const body = await readBody(req);
      const delivery = req.headers.get("x-otodock-delivery-id") || String(body.delivery_id || "");
      const claim = await claimOf(req);
      if (!claim || claim.principal !== "platform" || claim.handler !== name || claim.delivery !== delivery) {
        return json({ error: "not a platform claim for this handler" }, 400);
      }
      if (db.query("SELECT id FROM handled WHERE id = ?").get(delivery)) return new Response("already done");
      db.run("INSERT INTO handled (id, at) VALUES (?, ?)", [delivery, new Date().toISOString()]);
      if (name === "republish") { publish(); return json({ ok: true, cards: cards().length }); }
      return json({ error: "unknown handler" }, 404);
    }

    const basis = req.headers.get("x-otodock-basis") || "viewer";
    // Another app over a binding brings its own claim in X-OtoDock-Caller.
    const claim = await claimOf(req, basis === "binding" ? "x-otodock-caller" : "x-otodock-viewer");
    const who = nameOf(claim, basis);

    // The exported methods (app.json "exports"), served at /api/<method>
    // for another app over a binding and for an agent the app is placed in.
    // summary reads with no floor, so a nightly task can call it. move is
    // floored at editor: the platform judged that already, and the server
    // checks the claim again before the write.
    if (req.method === "GET" && url.pathname === "/api/summary") return json({ cards: summary() });
    if (req.method === "POST" && url.pathname === "/api/move") {
      if (!claim || claim.external || rankOf(claim.role) < rankOf("editor")) {
        return json({ error: "Moving a card needs the editor role." }, 403);
      }
      const body = await readBody(req);
      return moveCard(Number(body.id), body.col, undefined, who);
    }

    const writer = canWrite(claim, basis);
    if (req.method === "GET" && url.pathname === "/cards") return json({ cards: cards(), you: who, can_write: writer });
    if (req.method !== "GET" && !writer) return json({ error: "You can look at this board but not change it." }, 403);
    if (req.method === "POST" && url.pathname === "/cards") {
      const body = await readBody(req);
      const title = String(body.title ?? "").trim().slice(0, 200);
      const col = COLUMNS.includes(String(body.col)) ? String(body.col) : "todo";
      if (!title) return json({ error: "Write a title for the card." }, 400);
      const pos = (db.query("SELECT COALESCE(MAX(position), 0) + 1 AS p FROM cards WHERE col = ?").get(col) as any).p;
      db.run("INSERT INTO cards (title, col, position, moved_by, updated_at) VALUES (?, ?, ?, ?, ?)",
        [title, col, pos, who, new Date().toISOString()]);
      broadcast();
      return json({ ok: true });
    }
    const move = url.pathname.match(/^\/cards\/(\d+)\/move$/);
    if (req.method === "POST" && move) {
      const body = await readBody(req);
      return moveCard(Number(move[1]), body.col, body.position, who);
    }
    const del = url.pathname.match(/^\/cards\/(\d+)$/);
    if (req.method === "DELETE" && del) {
      const gone = db.run("DELETE FROM cards WHERE id = ?", [Number(del[1])]);
      if (!gone.changes) return json({ error: "That card is gone. Someone removed it." }, 404);
      broadcast();
      return json({ ok: true });
    }
    return json({ error: "not found" }, 404);
  },
  // An uncaught error: one line for the log, plain words for the caller.
  error(err) {
    console.log("request failed: " + String(err).slice(0, 300));
    return json({ error: "Something went wrong. Try again in a moment." }, 500);
  },
  websocket: {
    open(ws) { sockets.add(ws); ws.send(JSON.stringify({ type: "cards", cards: cards() })); },
    message() { /* the board is written over HTTP; the socket only pushes */ },
    close(ws) { sockets.delete(ws); },
  },
});
