// Parser for the delegation project's board file — the orchestrator-maintained
// markdown at projects/<project_id>/board.md (convention in the delegation
// skill). The overlay is a RENDERER of this file, not an editor: parsing is
// deliberately forgiving (missing sections → empty) so a hand-edited board
// never breaks the view.

export interface BoardLane {
  name: string
  /** Worker chat id or title fragment after the second `—` (may be ''). */
  ref: string
  /** Free-text status from the lane line (running | awaiting reply | …). */
  status: string
  done: boolean
}

export interface ProjectBoard {
  title: string
  goal: string
  status: string
  lanes: BoardLane[]
  decisions: string[]
  handoffs: string[]
}

const LANE_RE = /^-\s*\[( |x|X)\]\s*(.+)$/

/** Split a lane line's remainder on em-dash/hyphen separators:
 * `<name> — <chat ref> — <status>` (later fields optional). */
function splitLane(rest: string): [string, string, string] {
  const parts = rest.split(/\s+—\s+|\s+--\s+/)
  return [parts[0]?.trim() ?? '', parts[1]?.trim() ?? '', parts[2]?.trim() ?? '']
}

export function parseBoard(md: string): ProjectBoard {
  const board: ProjectBoard = {
    title: '', goal: '', status: '', lanes: [], decisions: [], handoffs: [],
  }
  let section = ''
  for (const raw of md.split('\n')) {
    const line = raw.trimEnd()
    if (line.startsWith('# ') && !board.title) {
      board.title = line.slice(2).trim()
      continue
    }
    if (line.startsWith('## ')) {
      section = line.slice(3).trim().toLowerCase()
      continue
    }
    // Header meta line(s): `Goal: ...   Status: ...` (same or separate lines).
    if (!section) {
      const goal = line.match(/goal:\s*(.+?)(\s{2,}status:|$)/i)
      if (goal && !board.goal) board.goal = goal[1].trim()
      const status = line.match(/status:\s*([a-z-]+)/i)
      if (status && !board.status) board.status = status[1].trim().toLowerCase()
      continue
    }
    if (section.startsWith('lane')) {
      const m = line.match(LANE_RE)
      if (m) {
        const [name, ref, status] = splitLane(m[2])
        board.lanes.push({ name, ref, status, done: m[1].toLowerCase() === 'x' })
      }
      continue
    }
    if (section.startsWith('decision')) {
      if (line.startsWith('- ')) board.decisions.push(line.slice(2).trim())
      continue
    }
    if (section.startsWith('hand')) {
      if (line.startsWith('- ')) board.handoffs.push(line.slice(2).trim())
    }
  }
  return board
}
