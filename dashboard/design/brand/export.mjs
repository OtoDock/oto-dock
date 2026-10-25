// The brand export: renders every public output of the OtoDock mark from
// the two masters beside this file (otodock-mark.svg, otodock-lockup.svg).
//
//   npm run brand:export             # write the outputs
//   npm run brand:export -- --check  # compare; exit 1 when one is stale
//
// The test suite calls checkOutputs() in memory. Public outputs only:
// nothing here names a private path, so the export ships in the public cut
// as is. Outputs are compared by their pixels, never their bytes: resvg's
// SIMD paths may round one step apart between processors, and zlib may
// encode the same pixels differently from one Node version to the next.
import { existsSync, readFileSync, realpathSync, writeFileSync } from 'node:fs'
import { createRequire } from 'node:module'
import { dirname, join } from 'node:path'
import { fileURLToPath } from 'node:url'
import { crc32, deflateSync, inflateSync } from 'node:zlib'

// The rasterizer is a native module, loaded on the first render only: the
// SVG helpers stay usable from a script that never renders (the token
// generator's reference page).
const require = createRequire(import.meta.url)
let resvg = null

export const BRAND = dirname(fileURLToPath(import.meta.url))
export const DASHBOARD = join(BRAND, '..', '..')
export const REPO = join(DASHBOARD, '..')

const palette = JSON.parse(readFileSync(join(DASHBOARD, 'design', 'tokens.json'), 'utf8')).color

/** The mark's blue (`color.blue.800`): the mark on light surfaces. */
export const MARK_BLUE = palette.blue['800'].$value
/** The bottom of the icon and boot field: the mark's blue at HSL lightness minus 6. */
export const MARK_DEEP = '#174067'
export const WHITE = '#ffffff'
/** The website's navy hero gradient: `linear-gradient(150deg, ...)`. */
export const HERO_ANGLE = 150
export const HERO_STOPS = [
  [0, palette.navy['950'].$value],
  [0.6, palette.navy['900'].$value],
  [1, palette.navy['700'].$value],
]
export const MARK_STATES = ['flat', 'white', 'small', 'glass', 'white-glass']
export const LOCKUP_STATES = ['flat', 'white']
/** The 96-unit frame's share of a tile: the 88-unit mark then fills 66
 *  percent of the tile's width, the same as the launcher icon's 52 dp frame
 *  in its visible 72 dp. */
export const TILE_FRAME = 52 / 72
/** The lockup's frame, clear space included. */
export const LOCKUP_RATIO = 412.558 / 123

export function readMaster(name = 'otodock-mark.svg') {
  return readFileSync(join(BRAND, name), 'utf8')
}

// Every state group opens as `<g id="<state>"`, the hidden ones with
// `display="none"` as the next attribute.
const STATE_GROUP = /<g id="([a-z-]+)"( display="none")?/g

/** The ids of a master's state groups and the ones it shows. */
export function stateGroups(text) {
  return [...text.matchAll(STATE_GROUP)].map((m) => ({ id: m[1], shown: !m[2] }))
}

function showState(text, state, color) {
  if (!stateGroups(text).some((g) => g.id === state)) throw new Error(`the master has no state "${state}"`)
  const shown = text.replace(STATE_GROUP, (_, id) => (id === state ? `<g id="${id}"` : `<g id="${id}" display="none"`))
  return color ? shown.replace(/^(<svg\b[^>]*?) color="[^"]*"/, `$1 color="${color}"`) : shown
}

/** The master with one state shown, in `color` (flat and small fill with it). */
export function markSvg(state = 'flat', { color = MARK_BLUE } = {}) {
  return showState(readMaster(), state, color)
}

/** The lockup with one state shown. */
export function lockupSvg(state = 'flat', { color = MARK_BLUE } = {}) {
  return showState(readMaster('otodock-lockup.svg'), state, color)
}

/** The `d` of a path in a master's `<defs>`. */
export function pathData(id, text = readMaster()) {
  const m = new RegExp(`<path id="${id}"[^>]*\\sd="([^"]+)"`).exec(text)
  if (!m) throw new Error(`the master has no path "${id}"`)
  return m[1]
}

const esc = (s) => s.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/"/g, '&quot;')

/** A bare `<svg>` element for an HTML page. The flat, white and small
 *  states are one path (`currentColor` for flat and small, so a CSS colour
 *  such as `text-brand-mark` paints them); the glass states carry the
 *  master's layers. */
export function inlineMark({ state = 'flat', size = 32, className, title = 'OtoDock' } = {}) {
  const cls = className ? ` class="${esc(className)}"` : ''
  const label = title ? ` role="img" aria-label="${esc(title)}"` : ' aria-hidden="true"'
  const open = `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 96 96" width="${size}" height="${size}"${cls}${label}>`
  if (state === 'flat' || state === 'white' || state === 'small') {
    const d = pathData(state === 'small' ? 'silhouette-small' : 'silhouette')
    const fill = state === 'white' ? WHITE : 'currentColor'
    return `${open}<path fill="${fill}" fill-rule="evenodd" d="${d}"/></svg>`
  }
  const body = markSvg(state).replace(/^<svg\b[^>]*>/, '').replace(/<\/svg>\s*$/, '')
  return `${open}${body.replace(/<title>[^<]*<\/title>/, '').replace(/<!--[\s\S]*?-->/g, '')}</svg>`
}

// --- Compositions -----------------------------------------------------------

/** Nest a standalone SVG at (x, y), `w` by `h`. */
export function place(svg, x, y, w, h) {
  return svg.replace(/^<svg\b([^>]*)>/, (_, attrs) =>
    `<svg${attrs.replace(/ (width|height)="[^"]*"/g, '')} x="${x}" y="${y}" width="${w}" height="${h}">`)
}

const stops = (list) => list.map(([o, c]) => `<stop offset="${o}" stop-color="${c}"/>`).join('')

/** The mark's field: `MARK_BLUE` at `y1` to `MARK_DEEP` at `y2`, vertical. */
export function fieldGradient(id, y1, y2) {
  return `<linearGradient id="${id}" gradientUnits="userSpaceOnUse" x1="0" y1="${y1}" x2="0" y2="${y2}">${stops([[0, MARK_BLUE], [1, MARK_DEEP]])}</linearGradient>`
}

/** The hero gradient over a `w` by `h` box, drawn the way CSS draws an
 *  angled `linear-gradient`: the line through the centre at the angle,
 *  long enough that the corners take the end colours. */
export function heroGradient(id, w, h) {
  const a = (HERO_ANGLE * Math.PI) / 180
  const dx = Math.sin(a)
  const dy = -Math.cos(a)
  const half = (Math.abs(w * dx) + Math.abs(h * dy)) / 2
  const r = (v) => Math.round(v * 1000) / 1000
  return `<linearGradient id="${id}" gradientUnits="userSpaceOnUse" x1="${r(w / 2 - dx * half)}" y1="${r(h / 2 - dy * half)}" x2="${r(w / 2 + dx * half)}" y2="${r(h / 2 + dy * half)}">${stops(HERO_STOPS)}</linearGradient>`
}

const doc = (w, h, body) =>
  `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 ${w} ${h}" width="${w}" height="${h}">${body}</svg>`

/** The mark on a transparent square, its 96-unit frame filling `size`. */
export function markOnClear(size, state = 'flat', color = MARK_BLUE) {
  return doc(size, size, place(markSvg(state, { color }), 0, 0, size, size))
}

/** The white mark on the mark's field: a `box`-wide square, rounded square
 *  (`radius`) or circle, centred in `size`. The field runs top to bottom of
 *  the box; the frame is `TILE_FRAME` of it. */
export function tileSvg(size, { box = size, shape = 'square', radius = 0, state = 'white' } = {}) {
  const o = (size - box) / 2
  const f = box * TILE_FRAME
  const clip = shape === 'circle'
    ? `<circle cx="${size / 2}" cy="${size / 2}" r="${box / 2}"/>`
    : `<rect x="${o}" y="${o}" width="${box}" height="${box}" rx="${radius}"/>`
  return doc(size, size,
    `<defs>${fieldGradient('field', o, o + box)}<clipPath id="tile">${clip}</clipPath></defs>` +
    `<g clip-path="url(#tile)"><rect x="${o}" y="${o}" width="${box}" height="${box}" fill="url(#field)"/>` +
    `${place(markSvg(state, { color: WHITE }), o + (box - f) / 2, o + (box - f) / 2, f, f)}</g>`)
}

/** The white lockup on the hero gradient, `lockupWidth` wide (clear space
 *  included), centred, or with its left edge at `left`. */
export function heroSvg(w, h, { lockupWidth, left } = {}) {
  const lh = lockupWidth / LOCKUP_RATIO
  const x = left ?? (w - lockupWidth) / 2
  return doc(w, h,
    `<defs>${heroGradient('hero', w, h)}</defs><rect width="${w}" height="${h}" fill="url(#hero)"/>` +
    place(lockupSvg('white', { color: WHITE }), x, (h - lh) / 2, lockupWidth, lh))
}

// --- Rendering and encoding ---------------------------------------------------

/** Render an SVG at its own size: straight (not premultiplied) RGBA. */
export function render(svg) {
  resvg ??= require('@resvg/resvg-js')
  const img = new resvg.Resvg(svg, { font: { loadSystemFonts: false } }).render()
  const px = Buffer.from(img.pixels)
  for (let i = 0; i < px.length; i += 4) {
    const a = px[i + 3]
    if (a === 0 || a === 255) continue
    for (let c = 0; c < 3; c++) px[i + c] = Math.min(255, Math.round((px[i + c] * 255) / a))
  }
  return { width: img.width, height: img.height, rgba: px }
}

function chunk(type, data) {
  const head = Buffer.alloc(4)
  head.writeUInt32BE(data.length)
  const body = Buffer.concat([Buffer.from(type, 'latin1'), data])
  const tail = Buffer.alloc(4)
  tail.writeUInt32BE(crc32(body) >>> 0)
  return Buffer.concat([head, body, tail])
}

const PNG_SIGNATURE = Buffer.from([0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a])

const paeth = (a, b, c) => {
  const p = a + b - c
  const pa = Math.abs(p - a)
  const pb = Math.abs(p - b)
  const pc = Math.abs(p - c)
  return pa <= pb && pa <= pc ? a : pb <= pc ? b : c
}

/** A PNG of an image from render(): RGBA, or RGB with `opaque` (which
 *  refuses an image with any transparency). Each row takes the filter
 *  with the smallest sum of absolute values. */
export function encodePng({ width, height, rgba }, { opaque = false } = {}) {
  const bpp = opaque ? 3 : 4
  const stride = width * bpp
  const rows = Buffer.alloc((stride + 1) * height)
  let prev = Buffer.alloc(stride)
  for (let y = 0; y < height; y++) {
    const cur = Buffer.alloc(stride)
    for (let x = 0; x < width; x++) {
      const s = (y * width + x) * 4
      if (opaque && rgba[s + 3] !== 255) throw new Error(`pixel ${x},${y} is not opaque`)
      rgba.copy(cur, x * bpp, s, s + bpp)
    }
    let best = null
    let bestSum = Infinity
    for (let f = 0; f < 5; f++) {
      const out = Buffer.alloc(stride)
      let sum = 0
      for (let i = 0; i < stride; i++) {
        const a = i >= bpp ? cur[i - bpp] : 0
        const b = prev[i]
        const c = i >= bpp ? prev[i - bpp] : 0
        const pred = f === 0 ? 0 : f === 1 ? a : f === 2 ? b : f === 3 ? (a + b) >> 1 : paeth(a, b, c)
        const v = (cur[i] - pred) & 0xff
        out[i] = v
        sum += v < 128 ? v : 256 - v
      }
      if (sum < bestSum) { bestSum = sum; best = [f, out] }
    }
    rows[y * (stride + 1)] = best[0]
    best[1].copy(rows, y * (stride + 1) + 1)
    prev = cur
  }
  const ihdr = Buffer.alloc(13)
  ihdr.writeUInt32BE(width, 0)
  ihdr.writeUInt32BE(height, 4)
  ihdr.set([8, opaque ? 2 : 6, 0, 0, 0], 8)
  return Buffer.concat([
    PNG_SIGNATURE,
    chunk('IHDR', ihdr),
    chunk('IDAT', deflateSync(rows, { level: 9 })),
    chunk('IEND', Buffer.alloc(0)),
  ])
}

/** Read a PNG this module wrote (8-bit RGB or RGBA, not interlaced):
 *  `{width, height, rgba, opaque}`. */
export function decodePng(buf) {
  if (!buf.subarray(0, 8).equals(PNG_SIGNATURE)) throw new Error('not a PNG')
  let pos = 8
  let width = 0
  let height = 0
  let type = 0
  const idat = []
  while (pos < buf.length) {
    const len = buf.readUInt32BE(pos)
    const name = buf.toString('latin1', pos + 4, pos + 8)
    const data = buf.subarray(pos + 8, pos + 8 + len)
    if (name === 'IHDR') {
      width = data.readUInt32BE(0)
      height = data.readUInt32BE(4)
      type = data[9]
      if (data[8] !== 8 || (type !== 2 && type !== 6) || data[12] !== 0) throw new Error('an unsupported PNG')
    } else if (name === 'IDAT') idat.push(data)
    pos += 12 + len
  }
  const bpp = type === 6 ? 4 : 3
  const stride = width * bpp
  const raw = inflateSync(Buffer.concat(idat))
  const rgba = Buffer.alloc(width * height * 4)
  let prev = Buffer.alloc(stride)
  for (let y = 0; y < height; y++) {
    const f = raw[y * (stride + 1)]
    const cur = Buffer.from(raw.subarray(y * (stride + 1) + 1, (y + 1) * (stride + 1)))
    for (let i = 0; i < stride; i++) {
      const a = i >= bpp ? cur[i - bpp] : 0
      const b = prev[i]
      const c = i >= bpp ? prev[i - bpp] : 0
      const pred = f === 0 ? 0 : f === 1 ? a : f === 2 ? b : f === 3 ? (a + b) >> 1 : paeth(a, b, c)
      cur[i] = (cur[i] + pred) & 0xff
    }
    for (let x = 0; x < width; x++) {
      const d = (y * width + x) * 4
      cur.copy(rgba, d, x * bpp, x * bpp + bpp)
      if (bpp === 3) rgba[d + 3] = 255
    }
    prev = cur
  }
  return { width, height, rgba, opaque: type === 2 }
}

/** An ICO of PNG frames (the way Pillow writes one; Windows reads PNG
 *  entries since Vista). */
export function encodeIco(frames) {
  const head = Buffer.alloc(6 + 16 * frames.length)
  head.writeUInt16LE(0, 0)
  head.writeUInt16LE(1, 2)
  head.writeUInt16LE(frames.length, 4)
  let offset = head.length
  frames.forEach(({ width, height, png }, i) => {
    const e = 6 + 16 * i
    head[e] = width >= 256 ? 0 : width
    head[e + 1] = height >= 256 ? 0 : height
    head.writeUInt16LE(1, e + 4)
    head.writeUInt16LE(32, e + 6)
    head.writeUInt32LE(png.length, e + 8)
    head.writeUInt32LE(offset, e + 12)
    offset += png.length
  })
  return Buffer.concat([head, ...frames.map((f) => f.png)])
}

/** The PNG frames of an ICO: `[{width, height, png}]`. */
export function decodeIco(buf) {
  if (buf.readUInt16LE(2) !== 1) throw new Error('not an ICO')
  return Array.from({ length: buf.readUInt16LE(4) }, (_, i) => {
    const e = 6 + 16 * i
    const at = buf.readUInt32LE(e + 12)
    return { width: buf[e] || 256, height: buf[e + 1] || 256, png: buf.subarray(at, at + buf.readUInt32LE(e + 8)) }
  })
}

// --- Outputs ------------------------------------------------------------------

/** A row: `path` (relative to the base the caller passes), and one of
 *  `svg` (a PNG of it; `opaque` writes RGB), `ico` (`[{size, svg}]`) or
 *  `text` (written as is). Every value is a function, built on demand. */
export const PUBLIC_OUTPUTS = [
  { path: 'dashboard/public/favicon.png', svg: () => markOnClear(64, 'small') },
  { path: 'dashboard/public/assets/favicon.png', svg: () => markOnClear(64, 'small') },
  { path: 'dashboard/public/logo.png', svg: () => tileSvg(1024), opaque: true },
  { path: 'dashboard/public/logo-192.png', svg: () => tileSvg(192), opaque: true },
  { path: 'dashboard/public/badge-72.png', svg: () => markOnClear(72, 'small', WHITE) },
  { path: '.github/media/logo-mark.png', svg: () => markOnClear(372) },
  { path: '.github/media/logo-mark-white.png', svg: () => markOnClear(372, 'white') },
  {
    path: 'satellite/bin/otodock.ico',
    // The Windows app icon sits on a light or a dark taskbar: the white
    // mark on the field, as on every app icon.
    ico: () => [
      { size: 16, svg: tileSvg(16, { radius: 3, state: 'small' }) },
      { size: 32, svg: tileSvg(32, { radius: 6, state: 'small' }) },
      { size: 48, svg: tileSvg(48, { radius: 9 }) },
      { size: 256, svg: tileSvg(256, { radius: 48 }) },
    ],
  },
]

/** The bytes a row writes. */
export function build(row) {
  if (row.text) return Buffer.from(row.text(), 'utf8')
  if (row.ico) return encodeIco(row.ico().map(({ size, svg }) => ({ width: size, height: size, png: encodePng(render(svg)) })))
  return encodePng(render(row.svg()), { opaque: Boolean(row.opaque) })
}

// Compared premultiplied: an edge pixel at alpha 2 whose alpha moves one
// step would otherwise read as a colour change of up to half the range.
function samePixels(a, b, tolerance) {
  if (a.width !== b.width || a.height !== b.height) return `${b.width}x${b.height}, expected ${a.width}x${a.height}`
  let worst = 0
  for (let i = 0; i < a.rgba.length; i += 4) {
    const aa = a.rgba[i + 3]
    const ba = b.rgba[i + 3]
    worst = Math.max(worst, Math.abs(aa - ba))
    for (let c = 0; c < 3; c++) worst = Math.max(worst, Math.abs((a.rgba[i + c] * aa) / 255 - (b.rgba[i + c] * ba) / 255))
  }
  return worst > tolerance ? `pixels differ by up to ${Math.round(worst)}` : null
}

/** Why the file at `path` is not what `row` writes, or null when current. */
export function compare(row, path, { tolerance = 2 } = {}) {
  if (!existsSync(path)) return 'missing'
  const have = readFileSync(path)
  if (row.text) return have.toString('utf8') === row.text() ? null : 'text differs'
  if (row.ico) {
    const frames = decodeIco(have)
    const want = row.ico()
    if (frames.map((f) => f.width).join() !== want.map((w) => w.size).join()) return `frames ${frames.map((f) => f.width)}`
    for (let i = 0; i < want.length; i++) {
      const why = samePixels(render(want[i].svg), decodePng(frames[i].png), tolerance)
      if (why) return `the ${want[i].size} px frame: ${why}`
    }
    return null
  }
  const got = decodePng(have)
  if (got.opaque !== Boolean(row.opaque)) return got.opaque ? 'RGB, expected RGBA' : 'RGBA, expected RGB'
  return samePixels(render(row.svg()), got, tolerance)
}

/** Write every row under `base` whose folder exists; the paths written. */
export function writeOutputs(rows, base = REPO) {
  const written = []
  for (const row of rows) {
    const path = join(base, row.path)
    if (!existsSync(dirname(path))) continue
    writeFileSync(path, build(row))
    written.push(row.path)
  }
  return written
}

/** `{stale: [[path, why]], skipped: [path]}` for the rows under `base`; a
 *  row whose folder is absent is skipped (the public cut's gate runs the
 *  test suite in a snapshot without some of them). */
export function checkOutputs(rows, base = REPO, opts) {
  const stale = []
  const skipped = []
  for (const row of rows) {
    const path = join(base, row.path)
    if (!existsSync(dirname(path))) { skipped.push(row.path); continue }
    const why = compare(row, path, opts)
    if (why) stale.push([row.path, why])
  }
  return { stale, skipped }
}

/** True when the module at `url` is the script node was started with.
 *  Node resolves a module's own URL through symlinks but leaves argv[1] as
 *  typed, so both sides are compared as real paths: a run through a
 *  symlinked folder must not skip its work and exit 0. */
export function isEntry(url) {
  const script = process.argv[1]
  return Boolean(script) && existsSync(script) && realpathSync(script) === realpathSync(fileURLToPath(url))
}

if (isEntry(import.meta.url)) {
  if (process.argv.includes('--check')) {
    const { stale } = checkOutputs(PUBLIC_OUTPUTS)
    for (const [p, why] of stale) console.error(`${p} is stale: ${why}`)
    if (stale.length) {
      console.error('run `npm run brand:export` in dashboard/ and commit the result')
      process.exit(1)
    }
    console.log(`${PUBLIC_OUTPUTS.length} outputs current`)
  } else {
    for (const p of writeOutputs(PUBLIC_OUTPUTS)) console.log(`wrote ${p}`)
  }
}
