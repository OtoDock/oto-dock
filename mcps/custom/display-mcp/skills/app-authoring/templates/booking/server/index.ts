// A booking page with its own customers: Bun + bun:sqlite, the reference
// server for an app that faces the public through a link. Visitors register
// and log in (the host keeps the login: the session token rides INSIDE the
// verified claim, never in a header the page sets), pick a slot, and pay
// through a Stripe Checkout Session the platform signs on the way out — the
// key never enters this sandbox. Stripe's event lands on /_handler/payment
// through the app's inbound hook, already verified; the owner is told on
// the app's own identity. The owner (an editor or above on the dashboard)
// sets the service, the price and the hours from the same page and sees
// every booking. Listen on 0.0.0.0:$PORT, answer GET /_health, write only
// under $OTODOCK_DATA_DIR.
import { Database } from "bun:sqlite";

const port = Number(process.env.PORT || 3000);
const dataDir = process.env.OTODOCK_DATA_DIR || "/app/data";
const proxy = process.env.OTODOCK_PROXY_URL || "";
const appId = process.env.OTODOCK_APP_ID || "";
const appToken = process.env.OTODOCK_APP_TOKEN || "";

const db = new Database(`${dataDir}/app.db`, { create: true });
db.exec("PRAGMA journal_mode = WAL");
// Additive migrations only (CREATE TABLE IF NOT EXISTS / ADD COLUMN): the
// previous release keeps running against the same file during a deploy.
db.exec(`CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL)`);
db.exec(`CREATE TABLE IF NOT EXISTS accounts (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  email TEXT NOT NULL UNIQUE,
  name TEXT NOT NULL,
  password_hash TEXT NOT NULL,
  created_at TEXT NOT NULL
)`);
db.exec(`CREATE TABLE IF NOT EXISTS sessions (
  token TEXT PRIMARY KEY,
  account_id INTEGER NOT NULL,
  expires_at TEXT NOT NULL
)`);
db.exec(`CREATE TABLE IF NOT EXISTS bookings (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  account_id INTEGER NOT NULL,
  starts_at TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'held',
  checkout_id TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL,
  paid_at TEXT NOT NULL DEFAULT ''
)`);
// A handler fire is retried with the SAME delivery id, so remember the ones
// already done (APPS.md "Handlers").
db.exec(`CREATE TABLE IF NOT EXISTS handled (id TEXT PRIMARY KEY, at TEXT NOT NULL)`);

// ── settings the owner edits from the page ─────────────────────────────────
// Generic on purpose: the service, the price, the hours. Times are wall
// clock in `timezone`; the page shows the label the server formats.
const DEFAULTS: Record<string, string> = {
  service_name: "A 30-minute session",
  price_cents: "0",
  currency: "eur",
  slot_minutes: "30",
  open_from: "09:00",
  open_to: "17:00",
  weekdays: "1,2,3,4,5",
  days_ahead: "14",
  hold_minutes: "30",
  timezone: "UTC",
};
function setting(key: string): string {
  const row = db.query("SELECT value FROM settings WHERE key = ?").get(key) as { value: string } | null;
  return row ? row.value : DEFAULTS[key];
}
function settings(): Record<string, string> {
  const out: Record<string, string> = {};
  for (const k of Object.keys(DEFAULTS)) out[k] = setting(k);
  return out;
}
function zoneOk(tz: string): boolean {
  try { new Intl.DateTimeFormat("en-US", { timeZone: tz }); return true; } catch { return false; }
}
// Each key checked on its own; a bad one is refused by name.
function saveSettings(body: Record<string, unknown>): string | null {
  const next: Record<string, string> = {};
  for (const k of Object.keys(DEFAULTS)) {
    if (!(k in body)) continue;
    const v = String(body[k] ?? "").trim();
    if (k === "service_name" && (!v || v.length > 120)) return "service name: 1 to 120 characters";
    if (k === "price_cents" && !/^\d{1,7}$/.test(v)) return "price: whole minor units (1250 for 12.50), at most 7 digits";
    if (k === "currency" && !/^[a-z]{3}$/i.test(v)) return "currency: a three-letter code";
    if ((k === "slot_minutes" || k === "hold_minutes" || k === "days_ahead") && !/^\d{1,3}$/.test(v)) return `${k}: a number`;
    if (k === "slot_minutes" && (Number(v) < 5 || Number(v) > 480)) return "slot length: 5 to 480 minutes";
    if (k === "hold_minutes" && (Number(v) < 10 || Number(v) > 720)) return "hold: 10 to 720 minutes";
    if (k === "days_ahead" && (Number(v) < 1 || Number(v) > 90)) return "days ahead: 1 to 90";
    if ((k === "open_from" || k === "open_to") && !/^([01]\d|2[0-3]):[0-5]\d$/.test(v)) return `${k}: HH:MM`;
    if (k === "weekdays" && !/^[1-7](,[1-7]){0,6}$/.test(v)) return "weekdays: ISO numbers, 1 (Monday) to 7 (Sunday)";
    if (k === "timezone" && !zoneOk(v)) return "timezone: an IANA name such as Europe/Athens";
    next[k] = k === "currency" ? v.toLowerCase() : v;
  }
  const from = next.open_from ?? setting("open_from"), to = next.open_to ?? setting("open_to");
  if (from >= to) return "opening hours: from must be before to";
  for (const [k, v] of Object.entries(next)) {
    db.run("INSERT INTO settings (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value", [k, v]);
  }
  return null;
}

// ── slots: wall-clock hours in a zone, as UTC instants ─────────────────────
function offsetMinutes(utc: Date, tz: string): number {
  const parts = new Intl.DateTimeFormat("en-US", {
    timeZone: tz, hourCycle: "h23", year: "numeric", month: "2-digit", day: "2-digit",
    hour: "2-digit", minute: "2-digit", second: "2-digit",
  }).formatToParts(utc);
  const p: Record<string, string> = {};
  for (const x of parts) p[x.type] = x.value;
  const asUtc = Date.UTC(+p.year, +p.month - 1, +p.day, +p.hour, +p.minute, +p.second);
  return Math.round((asUtc - utc.getTime()) / 60000);
}
function wallToUtc(y: number, m: number, d: number, hh: number, mm: number, tz: string): Date {
  const guess = Date.UTC(y, m - 1, d, hh, mm);
  const off = offsetMinutes(new Date(guess), tz);
  const off2 = offsetMinutes(new Date(guess - off * 60000), tz);   // a DST edge
  return new Date(guess - off2 * 60000);
}
function todayIn(tz: string): { y: number; m: number; d: number } {
  const p: Record<string, string> = {};
  for (const x of new Intl.DateTimeFormat("en-US", { timeZone: tz, year: "numeric", month: "2-digit", day: "2-digit" }).formatToParts(new Date())) p[x.type] = x.value;
  return { y: +p.year, m: +p.month, d: +p.day };
}
function label(iso: string, tz: string): string {
  return new Intl.DateTimeFormat("en-GB", { timeZone: tz, weekday: "short", day: "numeric", month: "short", hour: "2-digit", minute: "2-digit", hourCycle: "h23" }).format(new Date(iso));
}
type Slot = { starts_at: string; day: string; time: string };
function schedule(): Slot[] {
  const s = settings();
  const tz = s.timezone, step = Number(s.slot_minutes), days = Number(s.days_ahead);
  const allowed = new Set(s.weekdays.split(",").map(Number));
  const [fh, fm] = s.open_from.split(":").map(Number), [th, tm] = s.open_to.split(":").map(Number);
  const t0 = todayIn(tz);
  const out: Slot[] = [];
  const now = Date.now();
  for (let i = 0; i < days; i++) {
    const day = new Date(Date.UTC(t0.y, t0.m - 1, t0.d + i));
    const iso = day.getUTCDay() === 0 ? 7 : day.getUTCDay();
    if (!allowed.has(iso)) continue;
    const y = day.getUTCFullYear(), m = day.getUTCMonth() + 1, d = day.getUTCDate();
    for (let mins = fh * 60 + fm; mins + step <= th * 60 + tm; mins += step) {
      const at = wallToUtc(y, m, d, Math.floor(mins / 60), mins % 60, tz);
      if (at.getTime() <= now) continue;
      const hh = String(Math.floor(mins / 60)).padStart(2, "0"), mm = String(mins % 60).padStart(2, "0");
      out.push({ starts_at: at.toISOString(), day: new Intl.DateTimeFormat("en-GB", { timeZone: tz, weekday: "long", day: "numeric", month: "long" }).format(at), time: `${hh}:${mm}` });
    }
  }
  return out;
}

// ── bookings ───────────────────────────────────────────────────────────────
// A held booking waits for its payment; past the hold it is cancelled so
// the slot frees up. A free service confirms at once.
function expireHolds() {
  const cutoff = new Date(Date.now() - Number(setting("hold_minutes")) * 60000).toISOString();
  db.run("UPDATE bookings SET status = 'cancelled' WHERE status = 'held' AND created_at < ?", [cutoff]);
}
function taken(): Set<string> {
  expireHolds();
  const rows = db.query("SELECT starts_at FROM bookings WHERE status IN ('held', 'confirmed')").all() as { starts_at: string }[];
  return new Set(rows.map((r) => r.starts_at));
}
function bookingRow(id: number) {
  return db.query(`SELECT b.id, b.account_id, b.starts_at, b.status, b.checkout_id, b.created_at, b.paid_at, a.name, a.email
                   FROM bookings b JOIN accounts a ON a.id = b.account_id WHERE b.id = ?`).get(id) as any;
}
function shapeBooking(b: any, owner: boolean) {
  const tz = setting("timezone");
  const out: any = { id: b.id, starts_at: b.starts_at, label: label(b.starts_at, tz), status: b.status, paid_at: b.paid_at, created_at: b.created_at };
  if (owner) { out.name = b.name; out.email = b.email; }
  return out;
}

// ── who is calling ─────────────────────────────────────────────────────────
// The proxy signs the claim; only the proxy can reach this port, so decoding
// the payload is enough (verify with OTODOCK_APP_PUBLIC_KEY if you want to).
type Claim = { principal?: string; role?: string; external?: boolean; session?: string; username?: string; handler?: string; delivery?: string; kind?: string };
function claimOf(req: Request): Claim {
  try {
    const part = (req.headers.get("x-otodock-viewer") || "").split(".")[1] || "";
    return JSON.parse(Buffer.from(part.replace(/-/g, "+").replace(/_/g, "/"), "base64").toString("utf8"));
  } catch { return {}; }
}
// A customer is a link visitor whose claim carries the session the page set
// through the host (never a body field, never a header the page adds).
function customerOf(claim: Claim) {
  if (!claim.external || !claim.session) return null;
  const row = db.query(`SELECT a.id, a.name, a.email FROM sessions s JOIN accounts a ON a.id = s.account_id
                        WHERE s.token = ? AND s.expires_at > ?`).get(claim.session, new Date().toISOString()) as any;
  return row || null;
}
// The owner's view: a person on the dashboard with an editor's role or
// above (the owner of a personal app is its manager). A link visitor never.
function isOwner(claim: Claim, basis: string): boolean {
  if (claim.external || !(basis === "viewer" || basis === "agent")) return false;
  return ["editor", "manager", "admin"].includes(String(claim.role || ""));
}

const SESSION_DAYS = 30;   // keep level with external.session_days in app.json
function newSession(accountId: number): string {
  const token = Buffer.from(crypto.getRandomValues(new Uint8Array(32))).toString("base64url");
  const exp = new Date(Date.now() + SESSION_DAYS * 86400000).toISOString();
  db.run("INSERT INTO sessions (token, account_id, expires_at) VALUES (?, ?, ?)", [token, accountId, exp]);
  db.run("DELETE FROM sessions WHERE expires_at < ?", [new Date().toISOString()]);
  return token;
}
// Wrong passwords pace per email, on top of the platform's per-visitor and
// per-address limits.
const failures = new Map<string, { n: number; until: number }>();
function failedTooOften(email: string): boolean {
  const f = failures.get(email);
  return !!f && f.n >= 5 && f.until > Date.now();
}
function noteFailure(email: string) {
  const f = failures.get(email) || { n: 0, until: 0 };
  f.n += 1; f.until = Date.now() + 15 * 60000;
  failures.set(email, f);
}

// ── the platform: tell the owner, on the app's own identity ────────────────
// notifications.create with the launch token alone reaches the members of a
// shared app or the owner of a personal one (thirty an hour); declared as
// the `notify` platform action in app.json. Best-effort, never fatal.
async function notifyOwner(title: string, body: string) {
  if (!proxy || !appId || !appToken) return;
  try {
    const r = await fetch(`${proxy}/v1/apps/${appId}/platform/notifications.create`, {
      method: "POST", signal: AbortSignal.timeout(5000),
      headers: { authorization: `Bearer ${appToken}`, "content-type": "application/json" },
      body: JSON.stringify({ args: { title, body, severity: "success" } }),
    });
    if (!r.ok) console.log("owner not notified: " + r.status);
  } catch (e) { console.log("owner not notified: " + String(e).slice(0, 120)); }
}

// ── Stripe Checkout through the egress route ───────────────────────────────
// The platform swaps the app's Authorization for `STRIPE_SECRET_KEY` on the
// way to api.stripe.com (the secret's `sends_to`); the server holds no key.
async function createCheckout(booking: any, origin: string, returnPath: string): Promise<{ url: string; id: string } | { error: string }> {
  const s = settings();
  const form = new URLSearchParams();
  form.set("mode", "payment");
  form.set("line_items[0][price_data][currency]", s.currency);
  form.set("line_items[0][price_data][unit_amount]", s.price_cents);
  form.set("line_items[0][price_data][product_data][name]", `${s.service_name} — ${label(booking.starts_at, s.timezone)}`);
  form.set("line_items[0][quantity]", "1");
  form.set("client_reference_id", String(booking.id));
  form.set("metadata[booking_id]", String(booking.id));
  form.set("customer_email", booking.email);
  form.set("success_url", `${origin}${returnPath}?paid=${booking.id}`);
  form.set("cancel_url", `${origin}${returnPath}?cancelled=${booking.id}`);
  form.set("expires_at", String(Math.floor(Date.now() / 1000) + Math.max(30, Number(s.hold_minutes)) * 60));
  try {
    const r = await fetch(`${proxy}/v1/apps/${appId}/egress/api.stripe.com/v1/checkout/sessions`, {
      method: "POST", signal: AbortSignal.timeout(20000),
      headers: { authorization: `Bearer ${appToken}`, "content-type": "application/x-www-form-urlencoded" },
      body: form.toString(),
    });
    const doc = (await r.json().catch(() => ({}))) as any;
    if (!r.ok || !doc.url) {
      console.log(`checkout not created: ${r.status} ${String(doc?.error?.message || "").slice(0, 160)}`);
      return { error: r.status === 401 ? "payments are not set up on this app yet" : "the payment page could not be created" };
    }
    return { url: String(doc.url), id: String(doc.id) };
  } catch (e) {
    console.log("checkout not created: " + String(e).slice(0, 120));
    return { error: "the payment page could not be created" };
  }
}

// ── live: one ping per change; every page refetches its own view ───────────
const sockets = new Set<any>();
function changed() {
  const msg = JSON.stringify({ type: "changed" });
  for (const ws of sockets) { try { ws.send(msg); } catch { sockets.delete(ws); } }
}

const json = (body: unknown, status = 200) =>
  new Response(JSON.stringify(body), { status, headers: { "content-type": "application/json" } });
const EMAIL_RE = /^[^\s@]{1,64}@[^\s@]{1,120}\.[^\s@]{2,24}$/;

console.log(`booking listening on ${port}`);
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

    // The platform wakes a declared handler here and nothing else can. The
    // inbound hook's wake (X-OtoDock-Basis: inbound) carries Stripe's event,
    // already verified with the signing secret the platform holds.
    if (url.pathname.startsWith("/_handler/")) {
      const name = url.pathname.slice("/_handler/".length);
      const body = (await req.json().catch(() => ({}))) as any;
      const delivery = req.headers.get("x-otodock-delivery-id") || body.delivery_id || "";
      const claim = claimOf(req);
      if (claim.principal !== "platform" || claim.handler !== name || claim.delivery !== delivery) {
        return json({ error: "not a platform claim for this handler" }, 400);
      }
      if (db.query("SELECT id FROM handled WHERE id = ?").get(delivery)) return new Response("already done");
      db.run("INSERT INTO handled (id, at) VALUES (?, ?)", [delivery, new Date().toISOString()]);
      if (name === "payment") {
        let ev: any = {};
        try { ev = JSON.parse(String(body.payload?.body || "{}")); } catch { return json({ error: "not a JSON event" }, 400); }
        const obj = ev.data?.object || {};
        const id = Number(obj.client_reference_id || obj.metadata?.booking_id || 0);
        const b = id ? bookingRow(id) : null;
        // The event must name a booking AND its own Checkout Session.
        if (!b || !b.checkout_id || b.checkout_id !== String(obj.id || "")) return json({ ok: true, ignored: "no matching booking" });
        if (ev.type === "checkout.session.completed" && obj.payment_status === "paid" && b.status !== "confirmed") {
          db.run("UPDATE bookings SET status = 'confirmed', paid_at = ? WHERE id = ?", [new Date().toISOString(), b.id]);
          changed();
          await notifyOwner("Booking paid", `${b.name} booked ${label(b.starts_at, setting("timezone"))}`);
        } else if (ev.type === "checkout.session.expired" && b.status === "held") {
          db.run("UPDATE bookings SET status = 'cancelled' WHERE id = ?", [b.id]);
          changed();
        }
        return json({ ok: true });
      }
      return json({ error: "unknown handler" }, 404);
    }

    const basis = req.headers.get("x-otodock-basis") || "viewer";
    const claim = claimOf(req);
    const me = customerOf(claim);
    const owner = isOwner(claim, basis);
    const s = settings();
    const service = { service_name: s.service_name, price_cents: Number(s.price_cents), currency: s.currency, slot_minutes: Number(s.slot_minutes), timezone: s.timezone, hold_minutes: Number(s.hold_minutes) };

    if (req.method === "GET" && url.pathname === "/me") {
      return json({ me: me ? { name: me.name, email: me.email } : null, owner, link: !!claim.external, service });
    }
    if (req.method === "GET" && url.pathname === "/slots") {
      const busy = taken();
      return json({ service, slots: schedule().map((x) => ({ ...x, taken: busy.has(x.starts_at) })) });
    }

    // Accounts: register (behind the bot check the manifest asks for on
    // /register), log in, log out. The page then hands the token to the host.
    if (req.method === "POST" && url.pathname === "/register") {
      if (!claim.external) return json({ error: "customers register from the public link" }, 403);
      const body = (await req.json().catch(() => ({}))) as any;
      const email = String(body.email || "").trim().toLowerCase(), name = String(body.name || "").trim().slice(0, 80);
      const password = String(body.password || "");
      if (!EMAIL_RE.test(email)) return json({ error: "a valid email is required" }, 400);
      if (!name) return json({ error: "a name is required" }, 400);
      if (password.length < 8 || password.length > 200) return json({ error: "a password of at least 8 characters" }, 400);
      if (db.query("SELECT id FROM accounts WHERE email = ?").get(email)) return json({ error: "an account with this email exists — log in" }, 409);
      const hash = await Bun.password.hash(password);
      const r = db.run("INSERT INTO accounts (email, name, password_hash, created_at) VALUES (?, ?, ?, ?)", [email, name, hash, new Date().toISOString()]);
      return json({ token: newSession(Number(r.lastInsertRowid)), me: { name, email } });
    }
    if (req.method === "POST" && url.pathname === "/login") {
      if (!claim.external) return json({ error: "customers log in from the public link" }, 403);
      const body = (await req.json().catch(() => ({}))) as any;
      const email = String(body.email || "").trim().toLowerCase(), password = String(body.password || "");
      if (failedTooOften(email)) return json({ error: "too many attempts — try again in a while" }, 429);
      const acc = db.query("SELECT id, name, email, password_hash FROM accounts WHERE email = ?").get(email) as any;
      const ok = acc ? await Bun.password.verify(password, acc.password_hash) : false;
      if (!ok) { noteFailure(email); return json({ error: "wrong email or password" }, 401); }
      failures.delete(email);
      return json({ token: newSession(acc.id), me: { name: acc.name, email: acc.email } });
    }
    if (req.method === "POST" && url.pathname === "/logout") {
      if (claim.session) db.run("DELETE FROM sessions WHERE token = ?", [claim.session]);
      return json({ ok: true });
    }

    // The customer's bookings.
    if (req.method === "GET" && url.pathname === "/bookings") {
      if (!me) return json({ bookings: [] });
      expireHolds();
      const rows = db.query("SELECT * FROM bookings WHERE account_id = ? ORDER BY starts_at").all(me.id) as any[];
      return json({ bookings: rows.map((b) => shapeBooking(b, false)) });
    }
    if (req.method === "POST" && url.pathname === "/bookings") {
      if (!me) return json({ error: "log in to book" }, 401);
      const body = (await req.json().catch(() => ({}))) as any;
      const at = String(body.starts_at || "");
      if (!schedule().some((x) => x.starts_at === at)) return json({ error: "that slot is not offered" }, 400);
      if (taken().has(at)) return json({ error: "that slot was just taken" }, 409);
      const status = service.price_cents > 0 ? "held" : "confirmed";
      const r = db.run("INSERT INTO bookings (account_id, starts_at, status, created_at, paid_at) VALUES (?, ?, ?, ?, ?)",
        [me.id, at, status, new Date().toISOString(), status === "confirmed" ? new Date().toISOString() : ""]);
      const b = bookingRow(Number(r.lastInsertRowid));
      changed();
      if (status === "confirmed") await notifyOwner("New booking", `${b.name} booked ${label(b.starts_at, s.timezone)}`);
      return json({ booking: shapeBooking(b, false) });
    }
    // The payment page for a held booking: created through the egress route,
    // opened by the page with otodock.openExternal, and the visitor comes
    // back to the LINK (the page tells us its path; the origin is the
    // platform's own, from the forwarded headers) with ?paid=<id>.
    if (req.method === "POST" && url.pathname === "/checkout") {
      if (!me) return json({ error: "log in to book" }, 401);
      const body = (await req.json().catch(() => ({}))) as any;
      const b = bookingRow(Number(body.booking_id || 0));
      if (!b || b.account_id !== me.id) return json({ error: "no such booking" }, 404);
      if (b.status !== "held") return json({ error: `this booking is ${b.status}` }, 409);
      const returnPath = String(body.return_path || "");
      if (!/^\/s\/[A-Za-z0-9_-]{1,200}$/.test(returnPath)) return json({ error: "payments are made from the public link" }, 400);
      const origin = `${req.headers.get("x-forwarded-proto") || "https"}://${req.headers.get("x-forwarded-host") || ""}`;
      const out = await createCheckout(b, origin, returnPath);
      if ("error" in out) return json(out, 502);
      db.run("UPDATE bookings SET checkout_id = ? WHERE id = ?", [out.id, b.id]);
      return json({ url: out.url });
    }

    // The owner's view.
    if (url.pathname.startsWith("/owner/")) {
      if (!owner) return json({ error: "the owner's view is for editors and managers on the dashboard" }, 403);
      if (req.method === "GET" && url.pathname === "/owner/settings") return json({ settings: s });
      if (req.method === "POST" && url.pathname === "/owner/settings") {
        const body = (await req.json().catch(() => ({}))) as Record<string, unknown>;
        const err = saveSettings(body);
        if (err) return json({ error: err }, 400);
        changed();
        return json({ settings: settings() });
      }
      if (req.method === "GET" && url.pathname === "/owner/bookings") {
        expireHolds();
        const rows = db.query(`SELECT b.*, a.name, a.email FROM bookings b JOIN accounts a ON a.id = b.account_id
                               WHERE b.status <> 'cancelled' OR b.paid_at <> '' ORDER BY b.starts_at`).all() as any[];
        return json({ bookings: rows.map((b) => shapeBooking(b, true)), customers: (db.query("SELECT COUNT(*) AS n FROM accounts").get() as any).n });
      }
      const cancel = url.pathname.match(/^\/owner\/bookings\/(\d+)\/cancel$/);
      if (req.method === "POST" && cancel) {
        const r = db.run("UPDATE bookings SET status = 'cancelled' WHERE id = ? AND status <> 'cancelled'", [Number(cancel[1])]);
        if (!r.changes) return json({ error: "no such booking" }, 404);
        changed();
        return json({ ok: true });
      }
    }
    return json({ error: "not found" }, 404);
  },
  websocket: {
    open(ws) { sockets.add(ws); },
    message() { /* the socket only pings; everything is written over HTTP */ },
    close(ws) { sockets.delete(ws); },
  },
});
