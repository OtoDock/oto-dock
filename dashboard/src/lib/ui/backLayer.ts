// A layer that Back closes: the image viewer over a chat, a file's preview
// over an app. Two Backs reach it. The browser's Back pops a history entry
// the layer pushed when it opened. The Android app's back button and the
// iOS app's edge swipe run a script that reads two window members and calls
// the close (`MainActivity.handleBackAction`, the `__otodockLightboxActive`
// branch), before any of their own navigation.

interface BackBridge {
  __otodockLightboxActive?: boolean
  __otodockLightboxClose?: () => void
}

const HISTORY_KEY = 'otoLightbox'

/** Hold Back for a layer while it is open. `close` is read at the moment
 *  Back comes, so a caller passes a function that reads its latest close.
 *  The returned release takes the history entry back when the layer
 *  closed another way, and puts the members as they were before. */
export function holdBack(close: () => void): () => void {
  window.history.pushState({ [HISTORY_KEY]: true }, '', window.location.href)
  const onPop = () => close()
  window.addEventListener('popstate', onPop)
  const w = window as unknown as BackBridge
  const before = { active: w.__otodockLightboxActive, close: w.__otodockLightboxClose }
  w.__otodockLightboxActive = true
  w.__otodockLightboxClose = close
  return () => {
    window.removeEventListener('popstate', onPop)
    w.__otodockLightboxActive = before.active === true
    if (before.close) w.__otodockLightboxClose = before.close
    else delete w.__otodockLightboxClose
    const s = window.history.state
    if (s && typeof s === 'object' && (s as Record<string, unknown>)[HISTORY_KEY]) {
      window.history.back()
    }
  }
}
