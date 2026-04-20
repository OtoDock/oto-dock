import { useEffect, useImperativeHandle, useRef, type Ref } from 'react'

// Cloudflare Turnstile renders the login bot-protection widget. The script is loaded
// once via a module-level singleton that resolves only when window.turnstile is
// actually defined (tag present ≠ executed). The widget lives in its own component so
// its mount/unmount tracks the main login sub-form — LoginPage swaps sub-forms via
// early returns while staying mounted, so a parent-level effect wouldn't re-run.

const SCRIPT_SRC = 'https://challenges.cloudflare.com/turnstile/v0/api.js?onload=__otoTurnstileReady&render=explicit'

let loadPromise: Promise<void> | null = null

function loadTurnstile(): Promise<void> {
  if ((window as any).turnstile) return Promise.resolve()
  if (loadPromise) return loadPromise
  loadPromise = new Promise<void>((resolve) => {
    ;(window as any).__otoTurnstileReady = () => resolve()
    if (!document.querySelector('script[src^="https://challenges.cloudflare.com/turnstile"]')) {
      const s = document.createElement('script')
      s.src = SCRIPT_SRC
      s.async = true
      s.defer = true
      document.head.appendChild(s)
    }
  })
  return loadPromise
}

export interface TurnstileHandle {
  reset: () => void
}

interface TurnstileWidgetProps {
  siteKey: string
  onToken: (token: string) => void
  ref?: Ref<TurnstileHandle>
}

export function TurnstileWidget({ siteKey, onToken, ref }: TurnstileWidgetProps) {
  const containerRef = useRef<HTMLDivElement>(null)
  const widgetIdRef = useRef<string | undefined>(undefined)
  const onTokenRef = useRef(onToken)
  useEffect(() => { onTokenRef.current = onToken })

  useEffect(() => {
    if (!siteKey) return
    let cancelled = false
    loadTurnstile().then(() => {
      // cancelled guards the StrictMode mount→cleanup→mount double-invoke;
      // children.length is a backup against rendering twice into one element.
      if (cancelled || !containerRef.current || containerRef.current.children.length) return
      widgetIdRef.current = (window as any).turnstile.render(containerRef.current, {
        sitekey: siteKey,
        callback: (token: string) => onTokenRef.current(token),
        'expired-callback': () => onTokenRef.current(''),
        'error-callback': () => onTokenRef.current(''),
      })
    })
    return () => {
      cancelled = true
      if (widgetIdRef.current) {
        ;(window as any).turnstile?.remove(widgetIdRef.current)
        widgetIdRef.current = undefined
      }
    }
  }, [siteKey])

  useImperativeHandle(ref, () => ({
    reset: () => {
      if (widgetIdRef.current) (window as any).turnstile?.reset(widgetIdRef.current)
    },
  }))

  return <div ref={containerRef} className="mb-4" />
}
