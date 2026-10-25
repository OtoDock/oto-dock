// A usage window's reset instant in the viewer's clock (SubscriptionWindows).

/** "14:00" for today, "Wed 09:00" otherwise, in the viewer's clock. */
export function formatReset(iso: string | null): string {
  if (!iso) return ''
  const d = new Date(iso)
  if (Number.isNaN(d.getTime())) return ''
  const now = new Date()
  const time = d.toLocaleTimeString(undefined, { hour: '2-digit', minute: '2-digit' })
  if (d.toDateString() === now.toDateString()) return time
  return `${d.toLocaleDateString(undefined, { weekday: 'short' })} ${time}`
}
