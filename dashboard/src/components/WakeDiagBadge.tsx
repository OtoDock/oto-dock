// The wake-word diagnostics badge — visible only while the recorder is on
// (audio/wakeDiag.ts). Rendered once at the authenticated root beside the
// route outlet so it survives the wake navigation; Save writes the local
// file, Stop switches the recorder off and discards the buffer.

import { useState, useSyncExternalStore } from 'react'
import { getWakeDiagSnapshot, saveWakeDiag, setWakeDiag, subscribeWakeDiag } from '../audio/wakeDiag'
import Button from './ui/Button'

export default function WakeDiagBadge() {
  const snap = useSyncExternalStore(subscribeWakeDiag, getWakeDiagSnapshot, getWakeDiagSnapshot)
  const [note, setNote] = useState('')
  if (!snap.on) return null
  return (
    <div
      role="status"
      aria-label="Wake word diagnostics"
      className="fixed bottom-3 right-3 z-50 flex max-w-[95vw] flex-wrap items-center gap-2 rounded-full bg-surface-overlay px-3 py-1.5 text-xs text-primary shadow-elevation-3"
    >
      <span className={`inline-block h-2 w-2 rounded-full ${snap.listening ? 'bg-status-success-solid' : 'bg-status-warning-solid'}`} />
      <span>
        Wake diagnostics · {snap.listening ? 'listening' : 'microphone idle'} · {snap.events} events · {snap.seconds}s kept
      </span>
      <Button variant="primary" size="xs"
        type="button"
        onClick={() => setNote(saveWakeDiag())}
      >
        Save
      </Button>
      <Button variant="secondary" size="xs"
        type="button"
        onClick={() => { setNote(''); setWakeDiag(false) }}
      >
        Stop
      </Button>
      {note && <span className="text-tertiary">{note}</span>}
    </div>
  )
}
