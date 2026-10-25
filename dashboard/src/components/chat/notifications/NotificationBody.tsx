import { useEffect, useRef, useState } from 'react'
import { linkifyParts } from '../../../lib/linkify'

interface Props {
  body: string
  /** Tailwind clamp class for the collapsed state (e.g. 'line-clamp-2'). */
  clampClass?: string
}

/**
 * Renders a notification body with clickable http(s) links and a "Show more /
 * Show less" toggle that appears ONLY when the clamped text actually overflows.
 * Short notifications render exactly as before (no toggle). Link and toggle
 * clicks `stopPropagation` so they never trigger the surrounding row's
 * acknowledge / navigate handler.
 */
export default function NotificationBody({ body, clampClass = 'line-clamp-2' }: Props) {
  const ref = useRef<HTMLParagraphElement>(null)
  const [expanded, setExpanded] = useState(false)
  const [overflowing, setOverflowing] = useState(false)

  useEffect(() => {
    const el = ref.current
    if (!el || expanded) return
    // While clamped, scrollHeight > clientHeight iff the text is truncated.
    setOverflowing(el.scrollHeight > el.clientHeight + 1)
  }, [body, expanded])

  const parts = linkifyParts(body)

  return (
    <div>
      <p
        ref={ref}
        className={`text-xs text-secondary mt-0.5 break-words ${expanded ? '' : clampClass}`}
      >
        {parts.map((p, i) =>
          'url' in p ? (
            <a
              key={i}
              href={p.url}
              target="_blank"
              rel="noopener noreferrer"
              onClick={(e) => e.stopPropagation()}
              className="text-link underline break-all hover:text-link-hover"
            >
              {p.url}
            </a>
          ) : (
            <span key={i}>{p.text}</span>
          ),
        )}
      </p>
      {(overflowing || expanded) && (
        <button
          onClick={(e) => { e.stopPropagation(); setExpanded((v) => !v) }}
          className="mt-0.5 text-3xs font-medium text-link hover:text-link-hover"
        >
          {expanded ? 'Show less' : 'Show more'}
        </button>
      )}
    </div>
  )
}
