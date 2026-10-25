// The sender row's fallback name for a slug no agent list resolves.

/** "personal-assistant-lite" → "Personal Assistant Lite" (chat-header fallback). */
export function titleCaseSlug(slug: string): string {
  return slug.replace(/-/g, ' ').replace(/\b\w/g, (c) => c.toUpperCase())
}
