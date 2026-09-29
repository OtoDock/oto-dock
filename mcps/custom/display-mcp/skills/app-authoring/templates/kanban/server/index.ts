// A kanban board everyone writes: Bun + bun:sqlite, the reference server of
// the OtoDock app runtime. The platform runs this file on Bun inside the
// app's own sandbox; the only writable place is $OTODOCK_DATA_DIR
// (/app/data), the only way in is the proxy (X-OtoDock-Viewer names who is
// calling). Listen on 0.0.0.0:$PORT and answer GET /_health.
import { Database } from "bun:sqlite";

const port = Number(process.env.PORT || 3000);
const dataDir = process.env.OTODOCK_DATA_DIR || "/app/data";
const db = new Database(`${dataDir}/app.db`, { create: true });
db.exec("PRAGMA journal_mode = WAL");
// Additive migrations only: CREATE TABLE IF NOT EXISTS / ADD COLUMN — the
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
// ones already done (APPS.md "Handlers").
db.exec(`CREATE TABLE IF NOT EXISTS handled (id TEXT PRIMARY KEY, at TEXT NOT NULL)`);

const COLUMNS = ["todo", "doing", "done"];

type Viewer = { name: string; basis: string };

// The proxy signs the claim; the app may verify it with
// OTODOCK_APP_PUBLIC_KEY (Ed25519). Only the proxy can reach this port, so
// decoding the payload is enough for attribution.
function viewer(req: Request): Viewer {
  const basis = req.headers.get("x-otodock-basis") || "viewer";
  const raw = req.headers.get("x-otodock-viewer") || "";
  try {
    const part = raw.split(".")[1] || "";
    const json = JSON.parse(Buffer.from(part.replace(/-/g, "+").replace(/_/g, "/"), "base64").toString("utf8"));
    // An agent session names its agent and, when a person drives it, that
    // person too: say both, so a card the agent added is never mistaken
    // for one the person dragged.
    if (json.principal === "agent" || basis === "agent") {
      return { name: (json.username ? json.username + " via " : "") + "the agent", basis };
    }
    // No person behind these two: the platform waking a handler, and
    // another app calling over a binding. Say so instead of "someone".
    if (json.principal === "platform" || basis === "platform") {
      return { name: "the schedule", basis };
    }
    const name = json.username || (json.external ? "a link viewer" : "someone");
    return { name: String(name), basis };
  } catch {
    if (basis === "binding") {
      // A brokered call carries X-OtoDock-Caller, never a viewer claim.
      return { name: "another app", basis };
    }
    return { name: basis === "agent" ? "the agent" : "someone", basis };
  }
}

function cards() {
  return db.query("SELECT id, title, col, position, moved_by, updated_at FROM cards ORDER BY col, position, id").all();
}

const sockets = new Set<any>();
function broadcast() {
  const msg = JSON.stringify({ type: "cards", cards: cards() });
  for (const ws of sockets) {
    try { ws.send(msg); } catch { sockets.delete(ws); }
  }
  publish();
}

// What other apps may take (APPS.md "Bindings"): the board as a snapshot
// the platform serves from this file without waking us — written to a
// temporary name and renamed, so a reader never sees half a file — and one
// event per change, which wakes the apps that bound to us and listen.
// Both are declared in app.json under "exports"; best-effort, never fatal.
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
    const proxy = process.env.OTODOCK_PROXY_URL, app = process.env.OTODOCK_APP_ID, token = process.env.OTODOCK_APP_TOKEN;
    if (!proxy || !app || !token) return;
    try {
      await fetch(`${proxy}/v1/apps/${app}/events`, {
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
      return new Response("expected a websocket", { status: 400 });
    }
    // The platform wakes a declared handler here, and nothing else can:
    // /_handler/* is refused for every caller but the proxy. Verify the
    // claim names THIS handler and THIS delivery, drop a repeat, do the
    // work, answer within the minute (APPS.md "Handlers").
    if (url.pathname.startsWith("/_handler/")) {
      const name = url.pathname.slice("/_handler/".length);
      const body = (await req.json().catch(() => ({}))) as any;
      const delivery = req.headers.get("x-otodock-delivery-id") || body.delivery_id || "";
      let claim: any = {};
      try {
        const part = (req.headers.get("x-otodock-viewer") || "").split(".")[1] || "";
        claim = JSON.parse(Buffer.from(part.replace(/-/g, "+").replace(/_/g, "/"), "base64").toString("utf8"));
      } catch { claim = {}; }
      if (claim.principal !== "platform" || claim.handler !== name || claim.delivery !== delivery) {
        return json({ error: "not a platform claim for this handler" }, 400);
      }
      if (db.query("SELECT id FROM handled WHERE id = ?").get(delivery)) return new Response("already done");
      db.run("INSERT INTO handled (id, at) VALUES (?, ?)", [delivery, new Date().toISOString()]);
      if (name === "republish") { publish(); return json({ ok: true, cards: cards().length }); }
      return json({ error: "unknown handler" }, 404);
    }
    const who = viewer(req);
    if (req.method === "GET" && url.pathname === "/cards") return json({ cards: cards(), you: who.name });
    if (req.method === "POST" && url.pathname === "/cards") {
      const body = (await req.json().catch(() => ({}))) as { title?: string; col?: string };
      const title = String(body.title || "").trim().slice(0, 200);
      const col = COLUMNS.includes(String(body.col)) ? String(body.col) : "todo";
      if (!title) return json({ error: "title is required" }, 400);
      const pos = (db.query("SELECT COALESCE(MAX(position), 0) + 1 AS p FROM cards WHERE col = ?").get(col) as any).p;
      db.run("INSERT INTO cards (title, col, position, moved_by, updated_at) VALUES (?, ?, ?, ?, ?)",
        [title, col, pos, who.name, new Date().toISOString()]);
      broadcast();
      return json({ ok: true });
    }
    const move = url.pathname.match(/^\/cards\/(\d+)\/move$/);
    if (req.method === "POST" && move) {
      const body = (await req.json().catch(() => ({}))) as { col?: string; position?: number };
      const col = COLUMNS.includes(String(body.col)) ? String(body.col) : "todo";
      const position = Number.isFinite(Number(body.position)) ? Number(body.position) : 0;
      const changed = db.run("UPDATE cards SET col = ?, position = ?, moved_by = ?, updated_at = ? WHERE id = ?",
        [col, position, who.name, new Date().toISOString(), Number(move[1])]);
      if (!changed.changes) return json({ error: "no such card" }, 404);
      broadcast();
      return json({ ok: true });
    }
    const del = url.pathname.match(/^\/cards\/(\d+)$/);
    if (req.method === "DELETE" && del) {
      db.run("DELETE FROM cards WHERE id = ?", [Number(del[1])]);
      broadcast();
      return json({ ok: true });
    }
    return json({ error: "not found" }, 404);
  },
  websocket: {
    open(ws) { sockets.add(ws); ws.send(JSON.stringify({ type: "cards", cards: cards() })); },
    message() { /* the board is written over HTTP; the socket only pushes */ },
    close(ws) { sockets.delete(ws); },
  },
});
