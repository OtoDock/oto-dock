// Split a string into plain-text and http(s)-URL parts so notification bodies
// (and similar free text) can render real, clickable links instead of inert
// text. Pure + side-effect-free so it's trivially unit-testable.

export type LinkPart = { text: string } | { url: string }

// Match http/https URLs up to the first whitespace or a common closing
// delimiter. Notification bodies put the URL at the end with no trailing
// punctuation, so this is deliberately simple rather than RFC-exhaustive.
const URL_RE = /https?:\/\/[^\s<>"')\]]+/g

export function linkifyParts(text: string): LinkPart[] {
  if (!text) return []
  const parts: LinkPart[] = []
  let last = 0
  for (const m of text.matchAll(URL_RE)) {
    const start = m.index ?? 0
    if (start > last) parts.push({ text: text.slice(last, start) })
    parts.push({ url: m[0] })
    last = start + m[0].length
  }
  if (last < text.length) parts.push({ text: text.slice(last) })
  return parts
}
