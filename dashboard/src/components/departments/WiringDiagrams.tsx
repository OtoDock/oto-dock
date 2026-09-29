/**
 * The small diagrams on the department delegation cards. A mode diagram is
 * three levels of agents (a head, then two rows of two) with one straight
 * arrow between neighbouring levels and one between the agents of a level,
 * so each card differs only in which arrows exist and which way they point.
 * A reach diagram is three levels joined by the links the reach allows.
 * Decorative — the card's label and hint carry the meaning.
 */
import { useId } from 'react'
import type { DepartmentMode, DepartmentReach } from '../../lib/kinds/department'

type Pt = [number, number]

const R = 3.5 // node radius
const END = 5.5 // how far from a node's centre a link starts or ends
const STROKE = 1.6

function Node({ at, filled = false }: { at: Pt; filled?: boolean }) {
  return (
    <circle
      cx={at[0]}
      cy={at[1]}
      r={R}
      fill={filled ? 'currentColor' : 'none'}
      stroke="currentColor"
      strokeWidth={STROKE}
    />
  )
}

/** One arrowhead definition per diagram (the id must be unique per page). */
function ArrowDefs({ id }: { id: string }) {
  return (
    <defs>
      <marker
        id={id}
        markerUnits="userSpaceOnUse"
        markerWidth="5"
        markerHeight="5"
        refX="4.5"
        refY="2.5"
        orient="auto-start-reverse"
      >
        <path d="M0,0 L5,2.5 L0,5 z" fill="currentColor" />
      </marker>
    </defs>
  )
}

const HEAD: Pt = [48, 7]
const ROWS: Pt[][] = [
  [[30, 32], [66, 32]],
  [[30, 57], [66, 57]],
]
const AXIS = HEAD[0]

/** Which arrows the mode draws: between levels (down and/or up) and between
 * the agents of one level. */
const MODE_ARROWS: Record<DepartmentMode, { down: boolean; up: boolean; peers: boolean }> = {
  off: { down: false, up: false, peers: false },
  down: { down: true, up: false, peers: false },
  down_across: { down: true, up: false, peers: true },
  both: { down: true, up: true, peers: true },
}

export function ModeDiagram({ mode, className = '' }: { mode: DepartmentMode; className?: string }) {
  const id = useId()
  const arrow = `url(#${id})`
  const a = MODE_ARROWS[mode]
  const levelYs = [HEAD[1], ROWS[0][0][1], ROWS[1][0][1]]
  return (
    <svg viewBox="0 0 96 64" className={`w-28 h-auto mx-auto ${className}`} aria-hidden="true">
      <ArrowDefs id={id} />
      {(a.down || a.up) &&
        levelYs.slice(0, -1).map((y, i) => (
          <line
            key={`v${i}`}
            x1={AXIS}
            y1={y + END}
            x2={AXIS}
            y2={levelYs[i + 1] - END}
            stroke="currentColor"
            strokeWidth={STROKE}
            markerEnd={a.down ? arrow : undefined}
            markerStart={a.up ? arrow : undefined}
          />
        ))}
      {a.peers &&
        ROWS.map(([l, r], i) => (
          <line
            key={`p${i}`}
            x1={l[0] + END}
            y1={l[1]}
            x2={r[0] - END}
            y2={r[1]}
            stroke="currentColor"
            strokeWidth={STROKE}
            markerEnd={arrow}
            markerStart={arrow}
          />
        ))}
      <Node at={HEAD} filled />
      {ROWS.flat().map((p, i) => (
        <Node key={i} at={p} />
      ))}
    </svg>
  )
}

// Three levels in a column; the links show which levels are joined.
const LEVELS: Pt[] = [[42, 8], [42, 32], [42, 56]]

export function ReachDiagram({ reach, className = '' }: { reach: DepartmentReach; className?: string }) {
  const [top, mid, bottom] = LEVELS
  return (
    <svg viewBox="0 0 96 64" className={`w-28 h-auto mx-auto ${className}`} aria-hidden="true">
      {[[top, mid], [mid, bottom]].map(([a, b], i) => (
        <line
          key={i}
          x1={a[0]}
          y1={a[1] + END}
          x2={b[0]}
          y2={b[1] - END}
          stroke="currentColor"
          strokeWidth={STROKE}
        />
      ))}
      {reach === 'subtree' && (
        // The far pair is joined too: a bow around the middle level.
        <path
          d={`M ${top[0] + 3} ${top[1] + 4} Q ${top[0] + 38} ${mid[1]} ${bottom[0] + 3} ${bottom[1] - 4}`}
          fill="none"
          stroke="currentColor"
          strokeWidth={STROKE}
        />
      )}
      {LEVELS.map((p, i) => (
        <Node key={i} at={p} filled={i === 0} />
      ))}
    </svg>
  )
}
