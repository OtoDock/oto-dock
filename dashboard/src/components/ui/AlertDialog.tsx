import { useEffect, useId, useLayoutEffect, useRef, useState, type FormEvent, type MouseEvent, type ReactNode, type RefObject } from 'react'

import Button, { type ButtonVariant } from './Button'
import { DIALOG_PANEL } from './classes'
import Dialog from './Dialog'
import type { DialogTone } from './dialogs'
import { Input, Label } from './Field'

// The confirm and prompt panels: the dialog panel, narrower, with padding.
const PANEL = `${DIALOG_PANEL} max-w-sm p-5`

const CONFIRM_VARIANT: Record<DialogTone, ButtonVariant> = { default: 'primary', danger: 'danger' }
// Enter must not destroy: a dangerous confirm opens on Cancel.
const FOCUS_CANCEL: Record<DialogTone, boolean> = { default: false, danger: true }
// A click no person meant: the second click of the double click that opened
// the dialog, a press that began before it opened (a menu acting on
// mousedown), a reader's synthesized click while the opening key is held.
const SYNTHESIZED_GRACE_MS = 300

/** The clicks the buttons of a just-opened dialog take. */
function useDeliberateClicks(open: boolean) {
  const openedAtRef = useRef(0)
  const pressedRef = useRef(false)
  useLayoutEffect(() => {
    if (!open) return
    openedAtRef.current = performance.now()
    pressedRef.current = false
  }, [open])
  const onPointerDown = () => { pressedRef.current = true }
  const deliberate = (e: MouseEvent) => {
    if (e.detail > 1) return false
    if (e.detail === 0) return performance.now() - openedAtRef.current >= SYNTHESIZED_GRACE_MS
    return pressedRef.current
  }
  return { onPointerDown, deliberate }
}

/** Focus `ref` one frame after opening: a dialog opened from a key handler
 *  never receives that key's own activation (Chrome clicks the focused
 *  button on the keypress that follows the keydown). */
function useDeferredFocus(open: boolean, ref: RefObject<HTMLElement | null>) {
  useEffect(() => {
    if (!open) return
    const frame = requestAnimationFrame(() => ref.current?.focus())
    return () => cancelAnimationFrame(frame)
  }, [open, ref])
}

interface AlertDialogProps {
  open?: boolean
  title: ReactNode
  /** The sentence (line breaks kept) or a short list. */
  children?: ReactNode
  confirmLabel?: string
  cancelLabel?: string
  tone?: DialogTone
  /** The one-button form: an OK for a text that must be read. */
  hideCancel?: boolean
  busy?: boolean
  busyLabel?: string
  /** Why the last confirm failed, shown above the buttons. */
  error?: ReactNode
  confirmDisabled?: boolean
  onConfirm: () => void
  onCancel: () => void
  'data-testid'?: string
}

/**
 * A confirm or an alert: a title, a
 * sentence, Cancel and the action. For the declarative sites that own their
 * state (a busy line, an error, a confirm that must vanish with its
 * condition); the promise form is `useDialogs()`.
 */
export default function AlertDialog({
  open = true, title, children, confirmLabel = 'OK', cancelLabel = 'Cancel', tone = 'default',
  hideCancel = false, busy = false, busyLabel, error, confirmDisabled = false, onConfirm, onCancel,
  ...rest
}: AlertDialogProps) {
  const id = useId()
  const cancelRef = useRef<HTMLButtonElement>(null)
  const confirmRef = useRef<HTMLButtonElement>(null)
  const clicks = useDeliberateClicks(open)
  useDeferredFocus(open, FOCUS_CANCEL[tone] && !hideCancel ? cancelRef : confirmRef)

  return (
    <Dialog
      open={open}
      onClose={onCancel}
      busy={busy}
      role="alertdialog"
      aria-labelledby={`${id}-title`}
      aria-describedby={children ? `${id}-body` : undefined}
      data-testid={rest['data-testid']}
      panelClassName={PANEL}
    >
      <div onPointerDown={clicks.onPointerDown}>
        <h2 id={`${id}-title`} className="mb-2 text-base font-semibold text-primary">{title}</h2>
        {children && (
          <div id={`${id}-body`} className="mb-4 whitespace-pre-line text-sm text-secondary">{children}</div>
        )}
        {error && <p role="alert" className="mb-3 text-xs text-status-danger-fg">{error}</p>}
        <div className="flex justify-end gap-2">
          {!hideCancel && (
            <Button ref={cancelRef} onClick={(e) => { if (clicks.deliberate(e)) onCancel() }} disabled={busy}>
              {cancelLabel}
            </Button>
          )}
          <Button
            ref={confirmRef}
            variant={CONFIRM_VARIANT[tone]}
            onClick={(e) => { if (clicks.deliberate(e)) onConfirm() }}
            disabled={busy || confirmDisabled}
          >
            {busy && busyLabel ? busyLabel : confirmLabel}
          </Button>
        </div>
      </div>
    </Dialog>
  )
}

interface PromptDialogProps {
  title: string
  label: string
  defaultValue?: string
  placeholder?: string
  confirmLabel?: string
  validate?: (value: string) => string | null
  onSubmit: (value: string) => void
  onCancel: () => void
}

/** A name or a short text asked of the person (`useDialogs().prompt`). */
export function PromptDialog({
  title, label, defaultValue = '', placeholder, confirmLabel = 'OK', validate, onSubmit, onCancel,
}: PromptDialogProps) {
  const id = useId()
  const [value, setValue] = useState(defaultValue)
  const [touched, setTouched] = useState(false)
  const inputRef = useRef<HTMLInputElement>(null)
  const clicks = useDeliberateClicks(true)
  useDeferredFocus(true, inputRef)
  const refusal = validate ? validate(value) : null

  const submit = (e: FormEvent) => {
    e.preventDefault()
    setTouched(true)
    if (refusal) return
    onSubmit(value)
  }

  return (
    <Dialog
      onClose={onCancel}
      aria-labelledby={`${id}-title`}
      panelClassName={PANEL}
    >
      <form onSubmit={submit} onPointerDown={clicks.onPointerDown} noValidate>
        <h2 id={`${id}-title`} className="mb-3 text-base font-semibold text-primary">{title}</h2>
        <Label htmlFor={`${id}-field`}>{label}</Label>
        <Input
          ref={inputRef}
          id={`${id}-field`}
          value={value}
          placeholder={placeholder}
          invalid={touched && !!refusal}
          aria-describedby={touched && refusal ? `${id}-error` : undefined}
          onChange={(e) => setValue(e.target.value)}
          // The Enter that ends an input method's composition is the method's.
          onKeyDown={(e) => { if (e.key === 'Enter' && e.nativeEvent.isComposing) e.preventDefault() }}
        />
        <p id={`${id}-error`} role="alert" className="mt-1 min-h-[1rem] text-xs text-status-danger-fg">
          {touched ? refusal : null}
        </p>
        <div className="mt-3 flex justify-end gap-2">
          <Button onClick={(e) => { if (clicks.deliberate(e)) onCancel() }}>Cancel</Button>
          <Button type="submit" variant="primary">{confirmLabel}</Button>
        </div>
      </form>
    </Dialog>
  )
}
