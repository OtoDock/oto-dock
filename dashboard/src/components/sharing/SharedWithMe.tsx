import { useNavigate } from 'react-router-dom'
import { useMyShares } from '../../api/shares'

/**
 * "Shared with me" (SHARING.md): the apps and chats other people granted the
 * signed-in user, as one strip of chips. Rendered on the /agents page above
 * the view switcher and nowhere when there is nothing; a non-member of every
 * agent reaches their shared apps from here. Hidden shares stay out.
 */
export default function SharedWithMe() {
  const navigate = useNavigate()
  const { data } = useMyShares()
  const items = (data ?? []).filter((s) => !s.hidden)
  if (!items.length) return null
  return (
    <div className="flex shrink-0 items-center gap-2 overflow-x-auto border-b border-p-border-light bg-p-surface/60 px-4 py-2 scrollbar-hide">
      <span className="shrink-0 text-[11px] font-semibold uppercase tracking-wide text-p-text-light">Shared with me</span>
      {items.map((s) => (
        <button
          key={s.id}
          type="button"
          onClick={() => navigate(s.href)}
          title={`${s.title} — from ${s.shared_by_name || 'a colleague'}`}
          className="flex shrink-0 items-center gap-1.5 whitespace-nowrap rounded-full border border-p-accent-teal/30 bg-p-accent-teal/10 px-3 py-1 text-xs font-medium text-p-accent-teal transition-colors hover:bg-p-accent-teal/20"
        >
          <span>{s.title || (s.target_kind === 'app' ? 'an app' : 'a chat')}</span>
          {s.shared_by_name && <span className="font-normal opacity-70">· {s.shared_by_name}</span>}
        </button>
      ))}
    </div>
  )
}
