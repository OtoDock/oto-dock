/**
 * Scheme allowlist for hrefs built from agent / MCP / server-supplied URL
 * fields (chat blocks: url / link_url / download_url; media download links).
 * These are untrusted — a crafted `javascript:` or `data:` value would execute
 * in the dashboard origin when the user clicks the link. Returns the URL only
 * if it uses a safe scheme (http/https/mailto/blob) or is a same-origin
 * relative path; otherwise undefined, so the caller renders an <a> with no href
 * (not clickable) rather than an active script URL.
 */
export function safeHref(url: string | null | undefined): string | undefined {
  if (!url) return undefined
  const t = url.trim()
  if (!t) return undefined
  // Same-origin relative paths / fragments / queries are safe.
  if (/^(\/|\.{1,2}\/|#|\?)/.test(t)) return t
  try {
    const proto = new URL(t, window.location.origin).protocol
    if (proto === 'http:' || proto === 'https:' || proto === 'mailto:' || proto === 'blob:') return t
  } catch { /* unparseable → treat as unsafe */ }
  return undefined
}
