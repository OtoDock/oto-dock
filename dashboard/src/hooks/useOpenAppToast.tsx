import { useCallback, useEffect, useState } from 'react'
import type { NavigateFunction } from 'react-router-dom'
import { onOpenApp, type OpenAppFrame } from '../lib/appLive'
import OpenAppToast from '../components/apps/OpenAppToast'

// The shell's half of open_app (APPS.md "Live apps"): whichever page
// owns the socket feeds the appLive bus; a chat page that can show the app
// marks the frame handled synchronously, and this hook looks one tick later.
// What is left over becomes a notice with an Open button that goes to the
// app's own page. A hidden tab shows nothing; a notice fades by itself.

const AUTO_DISMISS_MS = 12_000

export function useOpenAppToast(navigate: NavigateFunction, enabled: boolean) {
  const [items, setItems] = useState<OpenAppFrame[]>([])
  const dismiss = useCallback((appId: string) => {
    setItems((cur) => cur.filter((x) => x.app_id !== appId))
  }, [])

  useEffect(() => {
    if (!enabled) return
    const timers = new Set<ReturnType<typeof setTimeout>>()
    const off = onOpenApp((f) => {
      const t = setTimeout(() => {
        timers.delete(t)
        if (f.handled) return
        if (typeof document !== 'undefined' && document.visibilityState !== 'visible') return
        setItems((cur) => [...cur.filter((x) => x.app_id !== f.app_id), f])
        const gone = setTimeout(() => { timers.delete(gone); dismiss(f.app_id) }, AUTO_DISMISS_MS)
        timers.add(gone)
      }, 0)
      timers.add(t)
    })
    return () => {
      off()
      timers.forEach((t) => clearTimeout(t))
    }
  }, [enabled, dismiss])

  if (!items.length) return null
  return (
    <OpenAppToast
      items={items}
      onDismiss={dismiss}
      onOpen={(f) => {
        dismiss(f.app_id)
        navigate(`/apps/${f.app_id}`)
      }}
    />
  )
}
