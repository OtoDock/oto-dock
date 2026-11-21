import { LIVE_DOT } from '../ui/classes'
import type { AccountLive } from '../../api/executionLayers'

/** What works on an account now, in its name row on both AI Engines lists:
 *  the pulsing dot, the count and how many run each model, most first ("7
 *  running · Opus 5.5 ×6, Fable 5.2 ×1"; one model that runs every session
 *  has no count, so a session with no model shows it: "2 running · Opus 5.5
 *  ×1"). On a phone the models take their own row under the name's. Nothing
 *  when nothing works: an idle session is shown nowhere. */
export default function AccountRunning({ live }: { live?: AccountLive | null }) {
  if (!live || live.sessions <= 0) return null
  const perModel = live.per_model ?? []
  const models = perModel.length > 1 || (perModel[0]?.sessions ?? 0) < live.sessions
    ? perModel.map((m) => `${m.name} ×${m.sessions}`).join(', ')
    : perModel[0]?.name ?? ''
  return (
    <>
      <span className="inline-flex items-center gap-1 text-xs text-secondary shrink-0">
        <span className={LIVE_DOT} aria-hidden="true" />
        {live.sessions} running
      </span>
      {models && (
        <span className="basis-full sm:basis-auto text-xs text-tertiary min-w-0">
          <span className="hidden sm:inline">· </span>{models}
        </span>
      )}
    </>
  )
}
