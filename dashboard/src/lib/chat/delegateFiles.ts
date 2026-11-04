// How a delegate's landed files are named in the delegating chat.

import { scopeWorkspace, userOf } from '../layout/tree'

/** A landed path as the delegating chat's workspace names it: the scope
 * workspace prefix (`workspace/`, `users/<u>/workspace/`) stripped, so the
 * label matches the callback note (`inbox/<worker>/report.md`). */
export function resultFileLabel(path: string): string {
  const prefix = `${scopeWorkspace(userOf(path))}/`
  return path.startsWith(prefix) ? path.slice(prefix.length) : path
}

/** A chip's short name for a landed file: its name with the last folder in
 * front and the rest shortened to `…` (`…/after/hero-1280.png`); the worker's
 * own inbox folder is left out, since the card names the worker
 * (`inbox/<worker>/report.md` reads `…/report.md`); a path with one folder
 * or none reads in full. The chip carries the full path in its title and the
 * expanded card lists it whole. */
export function resultFileChipLabel(path: string): string {
  let parts = resultFileLabel(path).split('/').filter(Boolean)
  let shortened = false
  if (parts[0] === 'inbox' && parts.length > 2) {
    parts = parts.slice(2)
    shortened = true
  }
  if (parts.length > 2) {
    parts = parts.slice(-2)
    shortened = true
  }
  return `${shortened ? '…/' : ''}${parts.join('/')}`
}

// How much of a name's end stays whole on a chip: the extension and, before
// it, the stem from its earliest separator within the last CHIP_TAIL_WINDOW
// characters (`-1280`, `-1280_1` for a renamed copy), else the stem's last
// CHIP_TAIL_CHARS characters.
const CHIP_TAIL_CHARS = 8
const CHIP_TAIL_WINDOW = 12

/** A chip's label in two parts, so a narrow card cuts it in the middle:
 * `head` may shrink to an ellipsis, `tail` never does. The tail is the end
 * of the file name (`-1280.png`, `-1280_1.txt`), the part that tells apart
 * two files whose names share their start; a label that fits shows whole. */
export function resultFileChipLabelParts(path: string): { head: string; tail: string } {
  const label = resultFileChipLabel(path)
  const name = label.slice(label.lastIndexOf('/') + 1)
  const dot = name.lastIndexOf('.')
  const stem = dot > 0 ? name.slice(0, dot) : name
  const from = Math.max(1, stem.length - CHIP_TAIL_WINDOW)
  const sep = stem.slice(from).search(/[-_ ]/)
  const cut = sep >= 0 ? from + sep : Math.max(0, stem.length - CHIP_TAIL_CHARS)
  const tailLength = name.length - cut
  return { head: label.slice(0, label.length - tailLength), tail: label.slice(label.length - tailLength) }
}
