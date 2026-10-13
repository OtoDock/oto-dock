import { useEffect, useRef, useState } from 'react'
import { pushEscHandler } from '../../lib/escStack'

interface Props {
  initial: string
  /** Validate the new name. Return an error message to keep the input open. */
  validate?: (next: string) => string | null
  onCommit: (next: string) => Promise<void> | void
  onCancel: () => void
}

/** Inline text input that replaces a filename. Submits on Enter, cancels on
 * Esc (via the precedence stack — context menu / preview don't collide).
 * Shows an inline error and keeps the input focused on validation/submit
 * failure.
 */
export default function InlineRename({ initial, validate, onCommit, onCancel }: Props) {
  const [value, setValue] = useState(initial)
  const [error, setError] = useState<string | null>(null)
  const [pending, setPending] = useState(false)
  const ref = useRef<HTMLInputElement>(null)

  useEffect(() => {
    ref.current?.focus()
    // Select the basename (before the last dot) so the user can keep the ext.
    const dot = initial.lastIndexOf('.')
    if (ref.current) {
      if (dot > 0) ref.current.setSelectionRange(0, dot)
      else ref.current.select()
    }
  }, [initial])

  useEffect(() => pushEscHandler(onCancel), [onCancel])

  const submit = async () => {
    const trimmed = value.trim()
    if (!trimmed || trimmed === initial) {
      onCancel()
      return
    }
    if (trimmed.includes('/') || trimmed.includes('\\') || trimmed === '.' || trimmed === '..') {
      setError('Invalid name')
      return
    }
    const err = validate?.(trimmed)
    if (err) {
      setError(err)
      return
    }
    setPending(true)
    setError(null)
    try {
      await onCommit(trimmed)
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Rename failed')
      setPending(false)
    }
  }

  return (
    <div className="flex flex-col gap-0.5 w-full">
      <input
        ref={ref}
        value={value}
        disabled={pending}
        onChange={(e) => { setValue(e.target.value); setError(null) }}
        onKeyDown={(e) => {
          if (e.key === 'Enter') { e.preventDefault(); submit() }
        }}
        onBlur={submit}
        className={`w-full text-xs px-1.5 py-0.5 rounded-sm border bg-white dark:bg-p-surface text-p-text ${
          error ? 'border-red-400' : 'border-brand'
        }`}
      />
      {error && (
        <span className="text-[10px] text-red-500">{error}</span>
      )}
    </div>
  )
}
