// Object URLs for blobs, shared by everyone showing the same blob: the URL
// is made when the first holder subscribes and revoked when the last one
// leaves, so a tile that unmounts never kills the URL a sibling tile of
// the same file still shows. A re-subscribe (StrictMode's double mount)
// after the last release gets a fresh URL, never a revoked one.
interface Held {
  url: string
  holders: number
}

const held = new Map<Blob, Held>()

/** The blob's URL while someone holds it; null before the first hold. */
export function blobUrl(blob: Blob): string | null {
  return held.get(blob)?.url ?? null
}

/** Hold the blob's URL (making it on the first hold); the return releases
 * it, revoking the URL with the last release. */
export function holdBlobUrl(blob: Blob): () => void {
  let entry = held.get(blob)
  if (!entry) {
    entry = { url: URL.createObjectURL(blob), holders: 0 }
    held.set(blob, entry)
  }
  entry.holders += 1
  let released = false
  return () => {
    if (released) return
    released = true
    entry.holders -= 1
    if (entry.holders > 0) return
    if (held.get(blob) === entry) held.delete(blob)
    URL.revokeObjectURL(entry.url)
  }
}
