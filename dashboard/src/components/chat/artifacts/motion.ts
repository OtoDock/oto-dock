// The motion tokens for code that animates through the Web Animations API.
// `tokens.css` declares them `@theme inline`, so Tailwind writes the values
// into its classes and no CSS variable carries them at run time: this is
// their mirror, kept in step by tests/artifactWindows.test.tsx.

import { useSyncExternalStore } from 'react'

/** `duration-fast`, `duration-base`, `duration-slow`, in ms. */
export const DURATION = { fast: 120, base: 200, slow: 320 } as const

/** `ease-standard`, `ease-enter`, `ease-exit`. */
export const EASE = {
  standard: 'cubic-bezier(0.2, 0, 0, 1)',
  enter: 'cubic-bezier(0, 0, 0, 1)',
  exit: 'cubic-bezier(0.3, 0, 1, 1)',
} as const

const QUERY = '(prefers-reduced-motion: reduce)'

function media(): MediaQueryList | null {
  return typeof window !== 'undefined' && typeof window.matchMedia === 'function'
    ? window.matchMedia(QUERY)
    : null
}

export function reducedMotion(): boolean {
  return media()?.matches ?? false
}

function subscribe(onChange: () => void): () => void {
  const mq = media()
  mq?.addEventListener?.('change', onChange)
  return () => mq?.removeEventListener?.('change', onChange)
}

/** The reduced-motion setting, re-rendering when it changes. */
export function useReducedMotion(): boolean {
  return useSyncExternalStore(subscribe, reducedMotion, () => false)
}

/** A motion may run: the browser animates elements and motion is not reduced. */
export function canAnimate(reduced: boolean = reducedMotion()): boolean {
  return !reduced && typeof Element !== 'undefined' && typeof Element.prototype.animate === 'function'
}
