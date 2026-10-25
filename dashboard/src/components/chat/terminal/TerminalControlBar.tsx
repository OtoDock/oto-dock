import { useEffect, useRef, useState } from 'react'
import { pushEscHandler } from '@/lib/escStack'
import { POPOVER } from '@/components/ui/classes'
import { GLASS, GLASS_HOVER, GLASS_PRESSED } from '@/components/ui/glass'
import { isCtrlHeld, setCtrlHold, subscribeCtrlHold } from '@/lib/terminalCtrlHold'

/**
 * The control-key bar for an interactive CLI session.
 * The highest-value terminal keys a remote/mobile keyboard can't reach, each
 * emitting the raw byte sequence to the PTY via `send`. Letters are left to
 * the device keyboard; this bar carries the navigation + control keys.
 *
 * Headliner: **⇧Tab** (`\x1b[Z`) cycles the native TUI permission mode. The **⋯**
 * button opens Ctrl plus the remaining navigation keys (Home/End/PgUp/PgDn/
 * Del/⌫) as a popover; Esc / tap-away closes. **Ctrl** is a one-shot sticky
 * modifier for keyboards without the key: arm it, type a letter, the terminal
 * receives the control byte (c → ^C). While armed a highlighted Ctrl chip
 * shows in the bar — tap it to disarm.
 */
interface Props {
  send: (seq: string) => void
  className?: string
}

const ESC = '\x1b'
const CR = '\r'
const TAB = '\t'
const BACKTAB = '\x1b[Z' // Shift+Tab (CSI Z)

const NAV_KEYS: Array<{ label: string; seq: string }> = [
  { label: 'Home', seq: '\x1b[H' },
  { label: 'End', seq: '\x1b[F' },
  { label: 'PgUp', seq: '\x1b[5~' },
  { label: 'PgDn', seq: '\x1b[6~' },
  { label: 'Del', seq: '\x1b[3~' },
  { label: '⌫', seq: '\x7f' },
]

// The key bar sits over the terminal beside the composer: chat chrome, so
// glass; a key that is on (Ctrl armed, the open ⋯) shows the pressed glass.
function keyBtn(pressed = false) {
  return (
    'shrink-0 min-w-[2.25rem] h-8 px-2 rounded-control text-xs font-medium text-primary ' +
    'active:scale-95 transition ' + (pressed ? GLASS_PRESSED : `${GLASS} ${GLASS_HOVER}`)
  )
}

export default function TerminalControlBar({ send, className }: Props) {
  const [expanded, setExpanded] = useState(false)
  const [ctrlArmed, setCtrlArmed] = useState(isCtrlHeld)
  const expandRef = useRef<HTMLDivElement | null>(null)

  useEffect(() => subscribeCtrlHold(setCtrlArmed), [])

  useEffect(() => {
    if (!expanded) return
    const pop = pushEscHandler(() => setExpanded(false))
    const onDown = (e: MouseEvent) => {
      if (expandRef.current && !expandRef.current.contains(e.target as Node)) setExpanded(false)
    }
    const t = setTimeout(() => document.addEventListener('mousedown', onDown), 0)
    return () => { pop(); clearTimeout(t); document.removeEventListener('mousedown', onDown) }
  }, [expanded])

  // mousedown-preventDefault keeps focus on the xterm textarea — the whole
  // point of arming Ctrl is that the NEXT device keystroke reaches the PTY.
  const toggleCtrl = () => {
    setCtrlHold(!isCtrlHeld())
    setExpanded(false)
  }

  return (
    <div ref={expandRef} className={`relative ${className || ''}`}>
      {expanded && (
        <div className={`absolute bottom-full left-0 right-0 z-50 mb-2 ${POPOVER} p-2.5`}>
          <div className="mb-2 flex items-center justify-between">
            <span className="text-2xs font-semibold text-tertiary">More keys</span>
            <button onClick={() => setExpanded(false)} className="text-tertiary hover:text-primary text-sm px-1">&times;</button>
          </div>
          <div className="flex flex-wrap gap-1.5">
            <button
              onMouseDown={(e) => e.preventDefault()}
              onClick={toggleCtrl}
              className={keyBtn(ctrlArmed)}
              title="Arm Ctrl for the next typed key (c → ^C)"
            >
              Ctrl
            </button>
            {NAV_KEYS.map((k) => (
              <button key={k.label} onClick={() => send(k.seq)} className={keyBtn()}>{k.label}</button>
            ))}
          </div>
        </div>
      )}

      <div className="flex items-center gap-1.5 overflow-x-auto py-1 scrollbar-hide">
        {ctrlArmed && (
          <button
            onMouseDown={(e) => e.preventDefault()}
            onClick={() => setCtrlHold(false)}
            className={keyBtn(true)}
            title="Ctrl is on. The next key you type gets Ctrl. Tap to turn it off."
          >
            Ctrl
          </button>
        )}
        <button onClick={() => send(ESC)} className={keyBtn()}>Esc</button>
        <button onClick={() => send(BACKTAB)} className={keyBtn()} title="Cycle permission mode">⇧Tab</button>
        <button onClick={() => send('\x1b[A')} className={keyBtn()}>↑</button>
        <button onClick={() => send('\x1b[B')} className={keyBtn()}>↓</button>
        <button onClick={() => send('\x1b[D')} className={keyBtn()}>←</button>
        <button onClick={() => send('\x1b[C')} className={keyBtn()}>→</button>
        <button onClick={() => send(TAB)} className={keyBtn()}>Tab</button>
        <button onClick={() => send(CR)} className={keyBtn()}>Enter</button>
        <button onClick={() => setExpanded((v) => !v)} className={keyBtn(expanded)} title="More keys">⋯</button>
      </div>
    </div>
  )
}
