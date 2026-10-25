import { useSyncExternalStore } from 'react'

// Below Tailwind's `md` (768 px) the settings areas show a phone's section
// list instead of the sidebar. Read as an external store so the first render
// already knows the width: a phone never sees the desktop page flash first,
// and a fold or an unfold re-renders the area in place.
const QUERY = '(max-width: 767px)'

function media(): MediaQueryList | null {
  return typeof window !== 'undefined' && typeof window.matchMedia === 'function'
    ? window.matchMedia(QUERY)
    : null
}

function subscribe(onChange: () => void): () => void {
  const mq = media()
  mq?.addEventListener?.('change', onChange)
  window.addEventListener('resize', onChange)
  return () => {
    mq?.removeEventListener?.('change', onChange)
    window.removeEventListener('resize', onChange)
  }
}

/** The viewport is under 768 px right now. */
export function isNarrowViewport(): boolean {
  const mq = media()
  return mq ? mq.matches : window.innerWidth < 768
}

export function useNarrow(): boolean {
  return useSyncExternalStore(subscribe, isNarrowViewport, () => false)
}
