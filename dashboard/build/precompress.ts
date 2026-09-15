import { promises as fs } from 'node:fs'
import path from 'node:path'
import { promisify } from 'node:util'
import zlib from 'node:zlib'

// Text assets worth a compressed sibling; fonts, images and audio are already
// compressed and are skipped by extension. Anything under 1 KB is not worth
// the extra file (the tiny Capacitor and OAuth chunks).
const COMPRESSIBLE = new Set(['.js', '.css', '.svg', '.json', '.txt', '.html'])
const MIN_BYTES = 1024

const brotli = promisify(zlib.brotliCompress)
const gzip = promisify(zlib.gzip)

/**
 * Write `<file>.br` and `<file>.gz` next to every compressible file under
 * `dir` (recursively) and return the paths written, sorted. The proxy serves
 * a sibling when the client accepts its encoding and the identity file
 * otherwise, so a directory without siblings still works. Brotli at its top
 * quality is slow (seconds for a large chunk); the async zlib calls run on
 * libuv's thread pool, so the files compress concurrently. A missing `dir`
 * is not an error: a build that emits no ui-kit simply has nothing to do.
 */
export async function precompressDir(dir: string): Promise<string[]> {
  const written: string[] = []
  const files = await walk(dir)
  await Promise.all(files.map(async (file) => {
    if (!COMPRESSIBLE.has(path.extname(file).toLowerCase())) return
    const source = await fs.readFile(file)
    if (source.length < MIN_BYTES) return
    const [br, gz] = await Promise.all([
      brotli(source, {
        params: {
          [zlib.constants.BROTLI_PARAM_QUALITY]: zlib.constants.BROTLI_MAX_QUALITY,
          [zlib.constants.BROTLI_PARAM_MODE]: zlib.constants.BROTLI_MODE_TEXT,
          [zlib.constants.BROTLI_PARAM_SIZE_HINT]: source.length,
        },
      }),
      gzip(source, { level: 9 }),
    ])
    await Promise.all([fs.writeFile(`${file}.br`, br), fs.writeFile(`${file}.gz`, gz)])
    written.push(`${file}.br`, `${file}.gz`)
  }))
  return written.sort()
}

async function walk(dir: string): Promise<string[]> {
  let entries
  try {
    entries = await fs.readdir(dir, { withFileTypes: true })
  } catch {
    return []
  }
  const out: string[] = []
  for (const entry of entries) {
    const full = path.join(dir, entry.name)
    if (entry.isDirectory()) out.push(...(await walk(full)))
    else if (entry.isFile()) out.push(full)
  }
  return out
}
