// Lightweight in-process pub/sub for swipe gestures forwarded OUT of
// sandboxed artifact/app iframes (same pattern as lib/fileUpdates).
//
// Iframes swallow touch events, so the dashboard's drawer gestures die when
// the finger lands on a mini-app/artifact. The injected runtime inside the
// frame (UI_RUNTIME, proxy-side) recognizes the same horizontal swipe as
// useSwipeGesture and posts `{type:'swipe', dir}`; the HOST component
// (AppFrame/UiArtifact) validates the source window and emits here with its
// iframe ELEMENT. useSwipeGesture subscribes and accepts only events whose
// frame is contained in its own container — identical semantics to a direct
// touch bubbling up the tree.

export interface IframeSwipe {
  /** The iframe the gesture came from — containment routing key. */
  el: HTMLElement
  dir: 'left' | 'right'
}

type Listener = (s: IframeSwipe) => void

const listeners = new Set<Listener>()

export function emitIframeSwipe(s: IframeSwipe): void {
  listeners.forEach((l) => {
    try {
      l(s)
    } catch {
      /* one subscriber throwing must not break the fan-out */
    }
  })
}

export function onIframeSwipe(listener: Listener): () => void {
  listeners.add(listener)
  return () => {
    listeners.delete(listener)
  }
}
