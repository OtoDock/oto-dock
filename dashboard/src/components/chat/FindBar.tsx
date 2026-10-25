import { useEffect, useRef } from 'react'

import { POPOVER } from '../ui/classes'
import { pushEscHandler } from '../../lib/escStack'
import { useSearch } from '../../contexts/useSearch'

interface Props {
  value: string
  onChange: (v: string) => void
  onClose: () => void
}

export default function FindBar({ value, onChange, onClose }: Props) {
  const { totalMatches, currentMatch, nextMatch, prevMatch } = useSearch()
  const inputRef = useRef<HTMLInputElement>(null)

  // Auto-focus on mount
  useEffect(() => {
    inputRef.current?.focus()
    inputRef.current?.select()
  }, [])

  // Escape closes — registered on the precedence stack so we never collide
  // with other panels that also bind Escape.
  useEffect(() => pushEscHandler(onClose), [onClose])

  const handleKeyDown = (e: React.KeyboardEvent) => {
    if (e.key === 'Enter') {
      e.preventDefault()
      if (e.shiftKey) prevMatch()
      else nextMatch()
    }
  }

  const displayIndex = totalMatches > 0 ? currentMatch + 1 : 0

  return (
    <div className={`absolute top-12 right-3 left-3 sm:left-auto sm:right-4 z-20 flex items-center gap-1.5 px-3 py-1.5
                    ${POPOVER} max-w-[calc(100vw-1.5rem)]`}>
      {/* Search input */}
      <input
        ref={inputRef}
        type="text"
        value={value}
        onChange={(e) => onChange(e.target.value)}
        onKeyDown={handleKeyDown}
        placeholder="Find in chat..."
        className="w-44 min-w-0 shrink text-xs px-2 py-1 rounded-control border border-strong bg-surface-card
                   text-primary placeholder:text-tertiary
                   focus:outline-hidden focus:ring-1 focus:ring-focus-ring/40 focus:border-action"
      />

      {/* Match counter */}
      <span className="text-2xs text-secondary whitespace-nowrap min-w-[4.5rem] text-center">
        {value.trim() ? `${displayIndex} of ${totalMatches}` : ''}
      </span>

      {/* Navigation buttons */}
      <button
        onClick={prevMatch}
        disabled={totalMatches === 0}
        className="p-1 rounded-sm hover:bg-surface-muted text-secondary hover:text-primary
                   disabled:opacity-30 disabled:cursor-default transition-colors"
        title="Previous match: Shift+Enter"
      >
        <svg className="w-3.5 h-3.5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
          <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M5 15l7-7 7 7" />
        </svg>
      </button>
      <button
        onClick={nextMatch}
        disabled={totalMatches === 0}
        className="p-1 rounded-sm hover:bg-surface-muted text-secondary hover:text-primary
                   disabled:opacity-30 disabled:cursor-default transition-colors"
        title="Next match: Enter"
      >
        <svg className="w-3.5 h-3.5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
          <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M19 9l-7 7-7-7" />
        </svg>
      </button>

      {/* Close button */}
      <button
        onClick={onClose}
        className="p-1 rounded-sm hover:bg-surface-muted text-tertiary hover:text-primary transition-colors"
        title="Close: Escape"
      >
        <svg className="w-3.5 h-3.5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
          <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M6 18L18 6M6 6l12 12" />
        </svg>
      </button>
    </div>
  )
}
