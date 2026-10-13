import {
  useEffect, useRef, useState, type CSSProperties, type KeyboardEvent as ReactKeyboardEvent, type RefObject,
} from 'react'

import { anchorBoxOf, usePopoverPlacement, type AnchorBox } from '../components/ui/popoverPosition'
import { pushEscHandler } from '../lib/escStack'

const ITEMS = '[role="menuitem"]:not([disabled])'

export interface PortalMenu {
  open: boolean
  buttonRef: RefObject<HTMLButtonElement | null>
  panelRef: RefObject<HTMLDivElement | null>
  /** The portaled panel's fixed position: hidden until it is measured. */
  style: CSSProperties
  toggle: () => void
  /** Close; `refocus` gives the focus back to the button (a keyboard close). */
  close: (refocus?: boolean) => void
  /** On the panel: arrows, Home and End move between the items, Tab closes. */
  onKeyDown: (e: ReactKeyboardEvent<HTMLElement>) => void
}

/**
 * A menu button with its panel portaled to the body (a toolbar's overflow
 * would clip it), usable by keyboard: the first item takes the focus once
 * the panel is placed, ArrowUp/ArrowDown/Home/End move it, Tab closes and
 * gives it back to the button, and Escape closes the menu alone (its handler
 * sits on top of the escape stack, above the panel it opened from). A press
 * outside closes it. The button carries `aria-haspopup="menu"` and
 * `aria-expanded={open}`; the panel `role="menu"`, its items
 * `role="menuitem"`. DocumentVersionsMenu is the radio-menu variant.
 */
export function usePortalMenu({ align = 'left' }: { align?: 'left' | 'right' } = {}): PortalMenu {
  const [anchor, setAnchor] = useState<AnchorBox | null>(null)
  const open = anchor !== null
  const buttonRef = useRef<HTMLButtonElement | null>(null)
  const panelRef = useRef<HTMLDivElement | null>(null)
  const placement = usePopoverPlacement(anchor, panelRef, { align })

  const close = (refocus = true) => {
    setAnchor(null)
    if (refocus) buttonRef.current?.focus()
  }
  const toggle = () => {
    if (anchor) { close(); return }
    const rect = buttonRef.current?.getBoundingClientRect()
    if (rect) setAnchor(anchorBoxOf(rect))
  }

  useEffect(() => {
    if (!open) return
    const onDown = (e: MouseEvent) => {
      const t = e.target as Node
      if (!panelRef.current?.contains(t) && !buttonRef.current?.contains(t)) setAnchor(null)
    }
    document.addEventListener('mousedown', onDown)
    const popEsc = pushEscHandler(() => {
      setAnchor(null)
      buttonRef.current?.focus()
    })
    return () => {
      document.removeEventListener('mousedown', onDown)
      popEsc()
    }
  }, [open])

  useEffect(() => {
    if (!open || !placement) return
    panelRef.current?.querySelector<HTMLElement>(ITEMS)?.focus()
  }, [open, placement])

  const onKeyDown = (e: ReactKeyboardEvent<HTMLElement>) => {
    const items = Array.from(panelRef.current?.querySelectorAll<HTMLElement>(ITEMS) ?? [])
    const at = items.indexOf(document.activeElement as HTMLElement)
    const move = (i: number) => { e.preventDefault(); items[(i + items.length) % items.length]?.focus() }
    if (e.key === 'ArrowDown') move(at + 1)
    else if (e.key === 'ArrowUp') move(at < 0 ? items.length - 1 : at - 1)
    else if (e.key === 'Home') move(0)
    else if (e.key === 'End') move(items.length - 1)
    else if (e.key === 'Tab') { e.preventDefault(); close() }
  }

  const style: CSSProperties = {
    position: 'fixed',
    left: placement?.left ?? anchor?.left ?? 0,
    top: placement?.top ?? anchor?.bottom ?? 0,
    visibility: placement ? 'visible' : 'hidden',
    zIndex: 60,
  }

  return { open, buttonRef, panelRef, style, toggle, close, onKeyDown }
}
