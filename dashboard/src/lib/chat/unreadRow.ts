// The chat list's unread tint by the age of the unanswered response.

// Unread-row age steps: a fresh response tints the whole row with the full
// action-subtle tint; one that has sat unread fades in two steps, so the sidebar
// separates "just finished" from "waiting since yesterday". A live store flip
// has no timestamp and counts as fresh (it IS fresh — the turn just ended).
export function unreadRowClass(lastResponseAt?: string | null): string {
  const ts = lastResponseAt ? Date.parse(lastResponseAt) : NaN
  if (Number.isNaN(ts)) return 'bg-action-subtle'
  const ageHours = (Date.now() - ts) / 3_600_000
  if (ageHours < 6) return 'bg-action-subtle'
  if (ageHours < 24) return 'bg-action-subtle/60'
  return 'bg-action-subtle/35'
}
