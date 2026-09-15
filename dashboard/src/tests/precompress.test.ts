// The build writes .br/.gz siblings next to every text asset; the proxy
// serves them by Accept-Encoding. Pins what gets a sibling and that the
// siblings decompress to the source.
import { mkdtempSync, mkdirSync, readFileSync, rmSync, writeFileSync } from 'node:fs'
import { tmpdir } from 'node:os'
import path from 'node:path'
import { brotliDecompressSync, gunzipSync } from 'node:zlib'
import { afterEach, describe, expect, it } from 'vitest'
import { precompressDir } from '../../build/precompress'

let dir: string
afterEach(() => { if (dir) rmSync(dir, { recursive: true, force: true }) })

describe('precompressDir', () => {
  it('writes both siblings for text assets over 1 KB, recursively, and skips the rest', async () => {
    dir = mkdtempSync(path.join(tmpdir(), 'precompress-'))
    const js = 'console.log("hello from the dashboard");\n'.repeat(60)
    writeFileSync(path.join(dir, 'index-XGlNw2dg.js'), js)
    writeFileSync(path.join(dir, 'tiny-AbCdEfGh.js'), 'export{}')
    writeFileSync(path.join(dir, 'font-AbCdEfGh.woff2'), Buffer.alloc(4096, 1))
    mkdirSync(path.join(dir, 'fonts'))
    writeFileSync(path.join(dir, 'fonts', 'LICENSES.txt'), 'MIT '.repeat(600))

    const written = await precompressDir(dir)

    expect(written).toEqual([
      path.join(dir, 'fonts', 'LICENSES.txt.br'),
      path.join(dir, 'fonts', 'LICENSES.txt.gz'),
      path.join(dir, 'index-XGlNw2dg.js.br'),
      path.join(dir, 'index-XGlNw2dg.js.gz'),
    ])
    expect(brotliDecompressSync(readFileSync(path.join(dir, 'index-XGlNw2dg.js.br'))).toString()).toBe(js)
    expect(gunzipSync(readFileSync(path.join(dir, 'index-XGlNw2dg.js.gz'))).toString()).toBe(js)
  })

  it('returns nothing for a directory that does not exist', async () => {
    dir = ''
    expect(await precompressDir(path.join(tmpdir(), 'no-such-dir-for-precompress'))).toEqual([])
  })
})
