/**
 * The one save indication of a control that saves on change. The owner (a
 * page, a section, a pill) holds one state keyed by the field that saved
 * last, so its rows show one indication at a time: `Saving…` once a save
 * runs past 300 ms, `Saved` for two seconds after it lands. A failure
 * toasts its reason (or hands it to the caller's `onFail`, a form that
 * shows its own error) and puts a toggle or a select back through `revert`.
 * `SaveState` in `components/ui/SettingsControls.tsx` draws the state.
 *
 * The save is a thunk returning the mutation's promise, and the API hooks
 * return their refetch from `onSuccess`, so `Saved` shows with the new
 * value on screen.
 *
 * A key names one value: a newer save of it decides, so an older failure of
 * the same key is skipped. Items saved apart (each agent of a list, each
 * library) take a key each, `group:item`, and one indication for the group
 * reads `stateOf('group')`, which shows any of its items.
 */
import { useCallback, useEffect, useRef, useState } from 'react'

import { toastError } from '../lib/http/apiJson'

/** `null` is the rest: nothing to show. */
export type SaveStatus = 'saving' | 'saved' | null

export const SAVING_AFTER_MS = 300
export const SAVED_FOR_MS = 2000

export type SaveOptions = (
  | { failTitle: string }
  | { onFail: (e: unknown) => void }
) & { revert?: () => void }

export interface SaveStateApi {
  stateOf: (key: string) => SaveStatus
  run: (key: string, save: () => Promise<unknown>, options: SaveOptions) => Promise<boolean>
  markSaved: (key: string) => void
}

type Shown = { key: string; status: SaveStatus }
const REST: Shown = { key: '', status: null }

export function useSaveState(): SaveStateApi {
  const [shown, setShownState] = useState<Shown>(REST)
  // An unchanged state renders nothing again.
  const setShown = useCallback((next: Shown) => {
    setShownState((prev) => (prev.key === next.key && prev.status === next.status ? prev : next))
  }, [])
  const seqRef = useRef(0)
  const currentRef = useRef(0)
  const latestRef = useRef(new Map<string, number>())
  const timerRef = useRef<ReturnType<typeof setTimeout> | undefined>(undefined)

  // A save that lands after the owner went shows nothing (its failure still
  // reverts and toasts).
  useEffect(() => () => {
    clearTimeout(timerRef.current)
    currentRef.current = ++seqRef.current
  }, [])

  const showSaved = useCallback((key: string) => {
    clearTimeout(timerRef.current)
    setShown({ key, status: 'saved' })
    timerRef.current = setTimeout(() => setShown(REST), SAVED_FOR_MS)
  }, [setShown])

  const run = useCallback(async (key: string, save: () => Promise<unknown>, options: SaveOptions) => {
    const token = ++seqRef.current
    currentRef.current = token
    latestRef.current.set(key, token)
    clearTimeout(timerRef.current)
    setShown(REST)
    timerRef.current = setTimeout(() => {
      if (currentRef.current === token) setShown({ key, status: 'saving' })
    }, SAVING_AFTER_MS)
    try {
      await save()
    } catch (e) {
      if (currentRef.current === token) {
        clearTimeout(timerRef.current)
        setShown(REST)
      }
      if (latestRef.current.get(key) === token) {
        options.revert?.()
        if ('onFail' in options) options.onFail(e)
        else toastError(options.failTitle)(e)
      }
      return false
    }
    if (currentRef.current === token) showSaved(key)
    return true
  }, [setShown, showSaved])

  // A save confirmed outside `run` is the key's latest too: an older run of
  // it that fails later must not put back what this one saved.
  const markSaved = useCallback((key: string) => {
    currentRef.current = ++seqRef.current
    latestRef.current.set(key, currentRef.current)
    showSaved(key)
  }, [showSaved])

  const stateOf = useCallback(
    (key: string): SaveStatus =>
      (shown.key === key || shown.key.startsWith(`${key}:`) ? shown.status : null),
    [shown],
  )

  return { stateOf, run, markSaved }
}
