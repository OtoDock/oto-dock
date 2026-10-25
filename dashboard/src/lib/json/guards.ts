// Narrowing a parsed JSON value (a frame, a response body, a thrown object)
// before a read.

/** A JSON object: not null, not an array. */
export function isRecord(v: unknown): v is Record<string, unknown> {
  return typeof v === 'object' && v !== null && !Array.isArray(v)
}
