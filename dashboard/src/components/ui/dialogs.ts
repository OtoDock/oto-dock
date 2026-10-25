import { useEffect, useId, useMemo, type ReactNode } from 'react'

/**
 * The page's confirm, alert and prompt, as
 * promises answered by the one `DialogsHost` mounted in App.tsx:
 *
 *   const dialogs = useDialogs()
 *   if (!(await dialogs.confirm({ title: 'Delete the task?', tone: 'danger', confirmLabel: 'Delete' }))) return
 *
 * A request belongs to the component that asked: when it unmounts (a route
 * change, a row that is gone) its open requests answer no and close, and a
 * request it makes after unmounting (an async handler still running)
 * answers no at once without showing, so an OK never acts for a page that
 * is no longer there.
 */

export type DialogTone = 'default' | 'danger'

export interface ConfirmOptions {
  title: string
  /** The sentence (line breaks kept) or a short list. */
  body?: ReactNode
  confirmLabel?: string
  cancelLabel?: string
  tone?: DialogTone
}

export interface AlertOptions {
  title: string
  body?: ReactNode
  okLabel?: string
}

export interface PromptOptions {
  title: string
  /** The field's label. */
  label: string
  defaultValue?: string
  placeholder?: string
  confirmLabel?: string
  /** The reason a value is refused, or null when it is fine. */
  validate?: (value: string) => string | null
}

/** The kinds of request, named once (the host and the store compare them). */
export const DIALOG_KIND = { CONFIRM: 'confirm', ALERT: 'alert', PROMPT: 'prompt' } as const

export type DialogRequest =
  | { kind: typeof DIALOG_KIND.CONFIRM; id: string; owner: string; options: ConfirmOptions; resolve: (ok: boolean) => void }
  | { kind: typeof DIALOG_KIND.ALERT; id: string; owner: string; options: AlertOptions; resolve: () => void }
  | { kind: typeof DIALOG_KIND.PROMPT; id: string; owner: string; options: PromptOptions; resolve: (value: string | null) => void }

export interface Dialogs {
  confirm: (options: ConfirmOptions) => Promise<boolean>
  alert: (options: AlertOptions) => Promise<void>
  prompt: (options: PromptOptions) => Promise<string | null>
}

let requests: readonly DialogRequest[] = []
const listeners = new Set<() => void>()
const alive = new Set<string>()
let seq = 0

function emit(): void {
  listeners.forEach((l) => l())
}

/** The no-answer of a request: what Cancel, Escape and an unmount give. */
function decline(r: DialogRequest): void {
  switch (r.kind) {
    case DIALOG_KIND.CONFIRM: r.resolve(false); break
    case DIALOG_KIND.ALERT: r.resolve(); break
    case DIALOG_KIND.PROMPT: r.resolve(null); break
  }
}

export function subscribeDialogs(listener: () => void): () => void {
  listeners.add(listener)
  return () => {
    listeners.delete(listener)
  }
}

export function dialogRequests(): readonly DialogRequest[] {
  return requests
}

function take(id: string): DialogRequest | null {
  const r = requests.find((x) => x.id === id) ?? null
  if (!r) return null
  requests = requests.filter((x) => x.id !== id)
  emit()
  return r
}

/** The host's answers. */
export function answerConfirm(id: string, ok: boolean): void {
  const r = take(id)
  if (r && r.kind === DIALOG_KIND.CONFIRM) r.resolve(ok)
}

export function answerAlert(id: string): void {
  const r = take(id)
  if (r && r.kind === DIALOG_KIND.ALERT) r.resolve()
}

export function answerPrompt(id: string, value: string | null): void {
  const r = take(id)
  if (r && r.kind === DIALOG_KIND.PROMPT) r.resolve(value)
}

function dropOwner(owner: string): void {
  const mine = requests.filter((r) => r.owner === owner)
  if (!mine.length) return
  requests = requests.filter((r) => r.owner !== owner)
  emit()
  mine.forEach(decline)
}

function add(r: DialogRequest): void {
  if (!alive.has(r.owner)) {
    decline(r)
    return
  }
  requests = [...requests, r]
  emit()
}

function dialogsFor(owner: string): Dialogs {
  const next = () => `dialog-${++seq}`
  return {
    confirm: (options) => new Promise((resolve) => add({ kind: DIALOG_KIND.CONFIRM, id: next(), owner, options, resolve })),
    alert: (options) => new Promise((resolve) => add({ kind: DIALOG_KIND.ALERT, id: next(), owner, options, resolve })),
    prompt: (options) => new Promise((resolve) => add({ kind: DIALOG_KIND.PROMPT, id: next(), owner, options, resolve })),
  }
}

export function useDialogs(): Dialogs {
  const owner = useId()
  useEffect(() => {
    alive.add(owner)
    return () => {
      alive.delete(owner)
      dropOwner(owner)
    }
  }, [owner])
  return useMemo(() => dialogsFor(owner), [owner])
}
