import { useLayoutEffect, useRef, type KeyboardEvent, type RefObject } from 'react'
import { flushSync } from 'react-dom'

import { hasEscEntryWithin } from '../../lib/escStack'
import { captureFocusOrigin, restoreFocus, type FocusOrigin } from '../ui/focusReturn'

// A text-like field: a checkbox or a radio is not where a person starts typing.
const FIELD = 'input:not([type="hidden"]):not([type="checkbox"]):not([type="radio"]):not([disabled]), select:not([disabled]), textarea:not([disabled])'

/** A finger as the primary pointer: a field focused by code would raise the
 *  on-screen keyboard over the form that just opened. No `matchMedia` (a
 *  test page) counts as a desktop. */
export function primaryPointerCoarse(): boolean {
  return typeof window !== 'undefined' && typeof window.matchMedia === 'function'
    && window.matchMedia('(pointer: coarse)').matches
}

/** For a block that appears where its button was pressed but takes no
 *  typing (a one-time secret to copy): scroll it into view and, on a
 *  desktop, focus its first button. A ref callback, run once on attach. */
export function revealOnAttach(el: HTMLElement | null): void {
  if (!el) return
  el.scrollIntoView?.({ block: 'nearest' })
  if (!primaryPointerCoarse()) el.querySelector<HTMLElement>('button:not([disabled])')?.focus({ preventScroll: true })
}

/**
 * A form opened where it was invoked (under its Add button, under its row).
 * Call it in the form's own component, mounted only while the form is open.
 *
 * On open the form scrolls into view and, on a desktop, its first field
 * takes the focus; on a phone the field stays the next tap. An Escape typed
 * inside the form cancels it and goes no further (the page's own Escape
 * handlers, a sidebar drawer's, never see it), unless a menu open inside the
 * form holds an Escape of its own; a portal child (a confirm opened from the
 * form) is not inside it. `close` closes through the caller's setter and
 * then gives the focus back to what opened the form, or to the neighbour of
 * the row it sat in when that row is gone, or to `fallback` when the opener
 * was replaced; only when the focus was in the form or fell to the body, so
 * a person who moved on while a save ran keeps their place. `formRef` is the
 * form's own element.
 */
export function useInvokedForm<T extends HTMLElement = HTMLDivElement>(
  formRef: RefObject<T | null>, fallback?: RefObject<HTMLElement | null>,
) {
  const originRef = useRef<FocusOrigin | null>(null)

  useLayoutEffect(() => {
    // StrictMode runs this twice: the second run would capture the field
    // the first one focused.
    if (!originRef.current) originRef.current = captureFocusOrigin(true)
    const el = formRef.current
    if (!el) return
    el.scrollIntoView?.({ block: 'nearest' })
    if (!primaryPointerCoarse()) el.querySelector<HTMLElement>(FIELD)?.focus({ preventScroll: true })
  }, [formRef])

  const close = (setClosed: () => void) => {
    const before = document.activeElement
    const wasInside = !!before && !!formRef.current?.contains(before)
    flushSync(setClosed)
    const now = document.activeElement
    const lost = wasInside || !now || now === document.body
    if (lost && originRef.current) restoreFocus(originRef.current, fallback)
  }

  const onKeyDown = (e: KeyboardEvent<T>, cancel: () => void) => {
    if (e.key !== 'Escape' || e.defaultPrevented) return
    // 229: the key code a browser reports while an input method holds the key.
    if (e.nativeEvent.isComposing || e.keyCode === 229) return
    if (!(e.target instanceof Node) || !e.currentTarget.contains(e.target)) return
    if (hasEscEntryWithin(e.currentTarget)) return
    e.preventDefault()
    e.stopPropagation()
    close(cancel)
  }

  return { close, onKeyDown }
}
