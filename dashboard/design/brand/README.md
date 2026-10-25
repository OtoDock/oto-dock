# The OtoDock mark

The robot head: a domed head with an antenna and two ear pads, a visor
whose lower edge steps up through an S-curve, a gear eye and a dot eye.
One colour. Every raster of the mark in this repository is rendered from
the masters in this folder; never edit an output by hand.

| File | What it is |
|---|---|
| `otodock-mark.svg` | The master: the geometry once, five states |
| `otodock-lockup.svg` | The mark beside the wordmark "OtoDock" |
| `export.mjs` | Renders the outputs below (`npm run brand:export` in `dashboard/`) |

## The grid

`viewBox 0 0 96 96` on a 24-unit grid, one unit 4 px. Filled shapes only,
no strokes. The silhouette is one compound path (`fill-rule` evenodd): the
head, the ear pads, the stem and the ball are one outer contour, the visor
is a hole, the eyes are islands inside it.

| Part | Units |
|---|---|
| Head | circle centred (12, 14), radius 9 |
| Antenna | stem 0.75 wide from the head's top; ball radius 1.25 at (12, 2) |
| Ear pads | x 1 to 4 and 20 to 23, y 11 to 16, corner radius 1 |
| Visor | x 4.5 to 19.5, top 9.5; the lower edge at 17 on the left, 16 on the right; corner radius 2.5 |
| S-curve | two tangent quarter arcs of radius 0.5 between x 12.5 and 13.5 |
| Gear eye | centred (8.5, 13): tip radius 2.5, root 1.8, hole 0.9, eight teeth |
| Dot eye | centred (15.5, 13), radius 1.25 |

The mark sits inside the frame with one unit to spare at the sides and
the bottom and 0.75 at the top.

## States

The master shows one state at a time; the others carry `display="none"`.
`export.mjs` switches them. The flat and small states fill with
`currentColor`: the root's `color` is the mark's blue, so the file shows
blue as an `<img>`, and a CSS colour wins on an inline copy (in the
dashboard, `text-brand-mark` gives the blue on light surfaces and white on
dark ones).

| State | Use |
|---|---|
| `flat` | The mark. Every size from 32 px, every icon |
| `white` | The same in white, for dark and coloured surfaces and for tools that do not resolve `currentColor` |
| `small` | The optical version for 16 and 24 px: the gear becomes a disc, the S-curve a straight slope, the ear pads and the antenna heavier. The favicon, the notification badges, the tray at small sizes |
| `glass` | The lit state: a gradient fill, a thin rim light along the top-left edges, a faint inner shadow and sheen, no outer glow. Hero sizes only |
| `white-glass` | The lit state for dark surfaces |

Size floors: the glass states from 96 px, and at 96 px only on 2x
displays (192 device pixels); below that the rim light is under a pixel
and the mark looks muddy. Flat everywhere else and in every icon. Small
at 16 and 24 px.

## Colours

- The mark: `#1d4f80` on light surfaces (`color.blue.800` in
  `design/tokens.json`), white on dark ones. Never navy, never the action
  blue. The mark and a primary button never share a white row.
- The field behind the white mark on an app icon, a store icon or a boot
  screen: `#1d4f80` at the top fading to `#174067` at the bottom (the same
  hue, HSL lightness minus 6).
- Navy is a surface colour (the website header, the hero sections, the
  sharing cards), never the mark's.

## The lockup

The wordmark is Comfortaa 700 outlined to a path, so no font is needed to
draw it. Cap height 12 units (two thirds of the head), the baseline on the
head's bottom tangent, the word one antenna height (4.25 units) right of
the ear pad. The file's frame carries the same clear space on every side.
States: `flat` and `white`.

## Outputs

`npm run brand:export` in `dashboard/` writes these; `-- --check` compares
them with the master and fails on a stale one, and the dashboard's test
suite runs the same comparison.

| Output | Size | What |
|---|---|---|
| `dashboard/public/favicon.png`, `dashboard/public/assets/favicon.png` | 64 | The tab icon: the small state in the mark's blue on transparent. One file under two names, kept revalidated by the server |
| `dashboard/public/logo.png` | 1024 | The white mark on the field: the first-run setup page |
| `dashboard/public/logo-192.png` | 192 | The same: the browser notification's icon |
| `dashboard/public/badge-72.png` | 72 | The white small mark on transparent: the browser notification's badge (drawn from its alpha) |
| `.github/media/logo-mark.png` | 372 | The mark in its blue: the README on a light page |
| `.github/media/logo-mark-white.png` | 372 | The white mark: the README on a dark page |
| `satellite/bin/otodock.ico` | 16, 32, 48, 256 | The remote machine's tray and uninstall icon: the white mark on the mark's field in a rounded square, the small state at 16 and 32, the flat mark above |

The app's launcher and notification icons, the website's marks and the
store graphics are rendered from the same master by a separate step that
reuses `export.mjs`.
