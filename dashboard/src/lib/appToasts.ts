// A toast raised outside the component tree (the deep-link handler
// completing an account connect on a page that was recreated meanwhile).
// In-process pub/sub like fileUpdates.ts: AppToastHost subscribes and shows
// it beside the delivered ones, with no inbox row behind it.

export interface AppToast {
  title: string
  body: string
  severity: 'info' | 'success' | 'warning' | 'danger'
}

type Listener = (t: AppToast) => void

const listeners = new Set<Listener>()

export function emitAppToast(t: AppToast): void {
  listeners.forEach((l) => {
    try {
      l(t)
    } catch {
      /* one subscriber throwing must not break the fan-out */
    }
  })
}

export function onAppToast(listener: Listener): () => void {
  listeners.add(listener)
  return () => {
    listeners.delete(listener)
  }
}
