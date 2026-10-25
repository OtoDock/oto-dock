import { GLASS_FILL } from '../ui/glass'

interface Participant {
  slug: string
  display_name: string
  color: string
}

interface Props {
  participants: Participant[]
  currentSpeaker: string | null
  leftParticipants?: Set<string>
}

export default function MeetingIndicator({ participants, currentSpeaker, leftParticipants }: Props) {
  if (participants.length === 0) return null

  return (
    <div className="flex justify-start pointer-events-none">
      <div className={`pointer-events-auto ${GLASS_FILL} border border-accent-agent-border rounded-full px-3 py-1 flex items-center gap-2`}>
        {/* Live dot + label */}
        <span className="flex items-center gap-1 text-3xs font-medium text-accent-agent-fg uppercase tracking-wide">
          <span className="relative inline-block w-1.5 h-1.5">
            <span className="absolute inset-0 rounded-full bg-accent-agent-solid animate-pulse" />
            <span className="absolute inset-0 rounded-full bg-accent-agent-solid" />
          </span>
          Meeting
        </span>

        <span className="w-px h-4 bg-border" />

        {/* Participant avatars */}
        <div className="flex items-center -space-x-1">
          {participants.map((p) => {
            const hasLeft = leftParticipants?.has(p.slug)
            const isSpeaking = !hasLeft && currentSpeaker === p.slug
            const initial = (p.display_name || p.slug || '?').charAt(0).toUpperCase()
            return (
              <div key={p.slug} className="relative" title={hasLeft ? `${p.display_name || p.slug}, left the meeting` : (p.display_name || p.slug)}>
                <div
                  className={`w-6 h-6 rounded-full flex items-center justify-center text-on-solid text-3xs font-bold border-2 transition-all duration-200 ${
                    hasLeft
                      ? 'border-surface-card opacity-30 grayscale'
                      : isSpeaking
                        ? 'border-accent-agent-solid scale-110 z-10 opacity-100'
                        : 'border-surface-card opacity-60'
                  }`}
                  style={{ backgroundColor: p.color || 'var(--color-accent-agent-fill)' }}
                >
                  {initial}
                </div>
                {isSpeaking && (
                  <span className="absolute -bottom-0.5 left-1/2 -translate-x-1/2 w-2 h-2 rounded-full bg-status-success-solid border border-surface-card">
                    <span className="absolute inset-0 rounded-full animate-ping bg-status-success-solid opacity-60" />
                  </span>
                )}
              </div>
            )
          })}
        </div>

        {/* Active speaker name */}
        {currentSpeaker && (() => {
          const speaker = participants.find(p => p.slug === currentSpeaker)
          if (!speaker) return null
          return (
            <span className="text-2xs font-medium text-secondary truncate max-w-[120px]">
              {speaker.display_name || speaker.slug}
            </span>
          )
        })()}
      </div>
    </div>
  )
}
