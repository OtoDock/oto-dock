// The wake-word diagnostics badge — visible only while the recorder is on
// (audio/wakeDiag.ts). Rendered once at the authenticated root beside the
// route outlet so it survives the wake navigation; Save writes the local
// file, Stop switches the recorder off and discards the buffer.

import { useState, useSyncExternalStore } from 'react'
import { getWakeDiagSnapshot, saveWakeDiag, setWakeDiag, subscribeWakeDiag } from '../audio/wakeDiag'

export default function WakeDiagBadge() {
  const snap = useSyncExternalStore(subscribeWakeDiag, getWakeDiagSnapshot, getWakeDiagSnapshot)
  const [note, setNote] = useState('')
  if (!snap.on) return null
  return (
    <div
      role="status"
      aria-label="Wake word diagnostics"
      className="fixed bottom-3 right-3 z-50 flex max-w-[95vw] flex-wrap items-center gap-2 rounded-full border border-p-border-light bg-p-surface px-3 py-1.5 text-xs text-p-text shadow-lg"
    >
      <span className={`inline-block h-2 w-2 rounded-full ${snap.listening ? 'bg-green-500' : 'bg-yellow-500'}`} />
      <span>
        Wake diagnostics · {snap.listening ? 'listening' : 'microphone idle'} · {snap.events} events · {snap.seconds}s kept
      </span>
      <button
        type="button"
        onClick={() => setNote(saveWakeDiag())}
        className="rounded-md bg-brand px-2 py-0.5 font-medium text-white"
      >
        Save
      </button>
      <button
        type="button"
        onClick={() => { setNote(''); setWakeDiag(false) }}
        className="rounded-md border border-p-border-light px-2 py-0.5 text-p-text-secondary hover:text-p-text"
      >
        Stop
      </button>
      {note && <span className="text-p-text-light">{note}</span>}
    </div>
  )
}
