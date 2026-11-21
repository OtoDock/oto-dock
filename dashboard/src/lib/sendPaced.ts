/**
 * A call the platform paced is told how long to wait: the host waits that
 * long (five seconds at most) and sends it once more, so no page writes a
 * retry of its own. A platform method, a button's call in the dashboard and
 * a link visitor's press all take this road. Pure: the share host's own
 * bundle imports it too.
 */

/** How long a refused call is told to wait: the body's milliseconds, else
 * the header's seconds. */
export async function pacedWaitMs(res: Response): Promise<{ wait: number; detail: string }> {
  const body = await res.json().catch(() => null)
  const told = Number(body?.retry_after_ms) || Number(res.headers.get('Retry-After')) * 1000 || 1000
  return { wait: Math.min(5000, Math.max(50, told)), detail: body?.detail || 'unavailable' }
}

/** `pacedFor` reads an answer's wait in milliseconds, 0 for an answer to
 * keep; the second answer is kept whatever it is. */
export async function sendPaced<T>(
  send: () => Promise<T>, pacedFor: (answer: T) => number | Promise<number>,
): Promise<T> {
  const first = await send()
  const wait = await pacedFor(first)
  if (!wait) return first
  await new Promise((r) => setTimeout(r, Math.min(5000, Math.max(50, wait)) + 20))
  return send()
}
