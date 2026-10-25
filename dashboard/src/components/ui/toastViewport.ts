// The one toast region's element, published by `ToastViewport` (mounted once
// in App.tsx) for the hosts that render toast cards into it: the app-wide
// AppToastHost and the chat page's notification toasts. It moves into the
// topmost open dialog while one is open, so the element can change.

type Listener = () => void

let viewport: HTMLElement | null = null
const listeners = new Set<Listener>()

export function setToastViewport(el: HTMLElement | null): void {
  if (viewport === el) return
  viewport = el
  listeners.forEach((l) => l())
}

export function toastViewport(): HTMLElement | null {
  return viewport
}

export function subscribeToastViewport(listener: Listener): () => void {
  listeners.add(listener)
  return () => {
    listeners.delete(listener)
  }
}
