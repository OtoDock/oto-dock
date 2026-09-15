// Build identity stamped into the page, so a running dashboard can tell
// whether the server now serves a different build (the proxy reads the same
// stamp out of dist/index.html and repeats it on the dashboard socket and
// on /health; src/lib/buildId.ts compares and reloads once).
//
// Deterministic on purpose: a hash of the FINAL index.html text (it names
// the entry chunk and the stylesheet by content hash, and the entry names
// every lazy chunk by hash, so any source change reaches it) plus the
// unhashed files copied from public/ (the wake-word worker, the service
// worker — index.html does not name those). Two builds of the same source
// get the same id: an image rebuilt from the same commit is not a new
// build and forces no reload.

import { createHash } from 'node:crypto'
import { readdirSync, readFileSync, statSync } from 'node:fs'
import path from 'node:path'
import type { HtmlTagDescriptor, Plugin } from 'vite'

export const BUILD_META_NAME = 'otodock-build'

function listFiles(dir: string): string[] {
  let names: string[]
  try {
    names = readdirSync(dir)
  } catch {
    return []
  }
  const out: string[] = []
  for (const name of names.sort()) {
    const full = path.join(dir, name)
    const st = statSync(full)
    if (st.isDirectory()) out.push(...listFiles(full))
    else if (st.isFile()) out.push(full)
  }
  return out
}

/** sha256 (first 16 hex chars) over the page text and the public files. */
export function computeBuildId(html: string, publicDir: string): string {
  const h = createHash('sha256')
  h.update(html)
  for (const file of listFiles(publicDir)) {
    h.update('\0' + path.relative(publicDir, file) + '\0')
    h.update(readFileSync(file))
  }
  return h.digest('hex').slice(0, 16)
}

export function buildIdTag(id: string): HtmlTagDescriptor {
  return { tag: 'meta', attrs: { name: BUILD_META_NAME, content: id }, injectTo: 'head' }
}

/** Vite plugin: inject `<meta name="otodock-build" content="<id>">` into
 * the built index.html. Build only — the dev server carries no stamp, so a
 * dev page never reloads on it. */
export function stampBuildId(): Plugin {
  let publicDir = path.resolve(__dirname, '..', 'public')
  return {
    name: 'otodock:stamp-build-id',
    apply: 'build',
    configResolved(config) {
      if (config.publicDir) publicDir = config.publicDir
    },
    transformIndexHtml: {
      // 'post': the html already names the hashed entry and stylesheet.
      order: 'post',
      handler(html) {
        return [buildIdTag(computeBuildId(html, publicDir))]
      },
    },
  }
}
