import { useState, useEffect, useRef } from 'react'
import { pushEscHandler } from '../../../lib/escStack'

interface TodoItem {
  content: string
  status: string
  activeForm?: string
}

interface Props {
  todos: TodoItem[]
}

export default function TodoPanel({ todos }: Props) {
  const [open, setOpen] = useState(false)
  const panelRef = useRef<HTMLDivElement>(null)

  // Defensive: never assume the prop is an array — a malformed/partial WS
  // frame must not crash (and unmount) the whole chat tree.
  const list = Array.isArray(todos) ? todos : []
  const total = list.length
  const completed = list.filter((t) => t.status === 'completed').length
  const hasPending = completed < total

  // Close on click outside
  useEffect(() => {
    if (!open) return
    const handler = (e: MouseEvent) => {
      if (panelRef.current && !panelRef.current.contains(e.target as Node)) {
        setOpen(false)
      }
    }
    document.addEventListener('mousedown', handler)
    return () => document.removeEventListener('mousedown', handler)
  }, [open])

  // Close on Escape (precedence stack)
  useEffect(() => {
    if (!open) return
    return pushEscHandler(() => setOpen(false))
  }, [open])

  // Auto-hide when all completed or empty (after all hooks)
  if (total === 0 || !hasPending) return null

  return (
    <div ref={panelRef}>
      {/* Icon button */}
      <button
        onClick={() => setOpen(!open)}
        title={`Checklist: ${completed}/${total} done`}
        className={`relative w-10 h-10 rounded-xl bg-white/80 dark:bg-gray-900/80 backdrop-blur-xs border border-p-border-light shadow-xs
                   hover:shadow-md hover:bg-white dark:hover:bg-p-surface transition-all flex items-center justify-center`}
      >
        {/* Checklist icon */}
        <svg className="w-5 h-5 text-p-text-secondary" viewBox="0 0 24 24" fill="none" stroke="currentColor">
          <rect x="4" y="3" width="16" height="18" rx="2" strokeWidth={1.5} />
          <path d="M8 9l1.5 1.5L12 8" strokeWidth={1.4} strokeLinecap="round" strokeLinejoin="round" />
          <path d="M14 9.5h3" strokeWidth={1.3} strokeLinecap="round" />
          <path d="M8 14h1.5" strokeWidth={1.5} strokeLinecap="round" />
          <circle cx="9" cy="14" r="1.2" strokeWidth={1.3} fill="none" />
          <path d="M14 14h3" strokeWidth={1.3} strokeLinecap="round" />
        </svg>
        {/* Progress badge — bottom-right */}
        <span className="absolute -bottom-1 -right-1 min-w-[18px] h-[18px] rounded-full bg-brand text-white text-[9px] font-bold flex items-center justify-center px-1 border-2 border-white dark:border-gray-900">
          {completed}/{total}
        </span>
      </button>

      {/* Dropdown panel */}
      {open && (
        <div className="mt-2 bg-white/95 dark:bg-gray-900/95 backdrop-blur-xs border border-p-border-light shadow-lg rounded-xl overflow-hidden
                        w-64 sm:w-72 max-w-[calc(100vw-2rem)]">
          {/* Header */}
          <div className="flex items-center justify-between px-3 py-2 border-b border-p-border-light bg-p-bg/50">
            <span className="text-xs font-medium text-p-text-secondary">Checklist</span>
            <span className="text-xs text-p-text-light font-mono">
              {completed}/{total}
            </span>
          </div>
          {/* Items */}
          <div className="max-h-64 overflow-y-auto py-1">
            {list.map((todo, i) => {
              const done = todo.status === 'completed'
              return (
                <div
                  key={i}
                  className="flex items-start gap-2 px-3 py-1.5 text-xs"
                >
                  <span className="mt-0.5 shrink-0">
                    {done ? (
                      <span className="text-p-success text-[11px]">&#10003;</span>
                    ) : (
                      <span className="inline-block w-3 h-3 rounded-full border-2 border-p-text-light" />
                    )}
                  </span>
                  <span className={done ? 'line-through text-p-text-light' : 'text-p-text'}>
                    {todo.content}
                  </span>
                </div>
              )
            })}
          </div>
        </div>
      )}
    </div>
  )
}
