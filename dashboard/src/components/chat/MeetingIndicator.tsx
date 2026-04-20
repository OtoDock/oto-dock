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
      <div className="pointer-events-auto bg-white/90 dark:bg-gray-900/90 backdrop-blur-xs border border-p-border-light rounded-full px-3 py-1 shadow-xs flex items-center gap-2">
        {/* Live dot + label */}
        <span className="flex items-center gap-1 text-[10px] font-medium text-[#0891b2] uppercase tracking-wide">
          <span className="relative inline-block w-1.5 h-1.5">
            <span className="absolute inset-0 rounded-full bg-[#0891b2] animate-pulse" />
            <span className="absolute inset-0 rounded-full bg-[#0891b2]" />
          </span>
          Meeting
        </span>

        <span className="w-px h-4 bg-p-border-light" />

        {/* Participant avatars */}
        <div className="flex items-center -space-x-1">
          {participants.map((p) => {
            const hasLeft = leftParticipants?.has(p.slug)
            const isSpeaking = !hasLeft && currentSpeaker === p.slug
            const initial = (p.display_name || p.slug || '?').charAt(0).toUpperCase()
            return (
              <div key={p.slug} className="relative" title={hasLeft ? `${p.display_name || p.slug} (left)` : (p.display_name || p.slug)}>
                <div
                  className={`w-6 h-6 rounded-full flex items-center justify-center text-white text-[10px] font-bold border-2 transition-all duration-200 ${
                    hasLeft
                      ? 'border-white dark:border-gray-900 opacity-30 grayscale'
                      : isSpeaking
                        ? 'border-[#0891b2] scale-110 z-10 opacity-100'
                        : 'border-white dark:border-gray-900 opacity-60'
                  }`}
                  style={{ backgroundColor: p.color || '#6B7280' }}
                >
                  {initial}
                </div>
                {isSpeaking && (
                  <span className="absolute -bottom-0.5 left-1/2 -translate-x-1/2 w-2 h-2 rounded-full bg-green-500 border border-white dark:border-gray-900">
                    <span className="absolute inset-0 rounded-full animate-ping bg-green-400 opacity-60" />
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
            <span className="text-[11px] font-medium text-p-text-secondary truncate max-w-[120px]">
              {speaker.display_name || speaker.slug}
            </span>
          )
        })()}
      </div>
    </div>
  )
}
