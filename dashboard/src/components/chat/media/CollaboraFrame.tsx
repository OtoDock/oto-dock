import { useEffect, useId, useRef, type CSSProperties, type Ref, type SyntheticEvent } from 'react'

/** The editor frame's sandbox: Collabora runs scripts, keeps its own
 * same-origin state, posts its dialogs' forms and opens the links and
 * downloads a document carries in a normal tab. Served from the dashboard's
 * own origin (sub-path mode) the attribute does not isolate it; the frame
 * holds a one-file WOPI token either way. */
export const COLLABORA_SANDBOX =
  'allow-scripts allow-same-origin allow-forms allow-popups allow-popups-to-escape-sandbox'

/** What a mint answers: the host page URL and the token posted into it. */
export interface CollaboraFrameData {
  url: string
  token?: string | null
  ttl?: number | null
}

interface Props {
  /** The Collabora host page URL (``cool.html?WOPISrc=…``), no token on it. */
  url: string
  /** The WOPI token, posted into the frame with a hidden form (Collabora's
   * host page shape) so it never rides a URL a log or the history keeps. */
  accessToken?: string | null
  accessTokenTtl?: number | null
  /** A new value reloads the frame (a new post with the same token). */
  frameKey?: string | number
  iframeRef?: Ref<HTMLIFrameElement>
  className?: string
  style?: CSSProperties
  onLoad?: () => void
}

/** The Collabora editor frame. With a token it starts blank and the form
 * posts into it; without one (a URL that still carries its token, from
 * before the token left the URL) it loads the URL as is. */
export default function CollaboraFrame({
  url, accessToken, accessTokenTtl, frameKey = 0, iframeRef, className, style, onLoad,
}: Props) {
  const name = `collabora-${useId().replace(/[^A-Za-z0-9_-]/g, '')}`
  const formRef = useRef<HTMLFormElement | null>(null)
  const posted = !!accessToken

  useEffect(() => {
    if (posted) formRef.current?.submit()
  }, [posted, url, accessToken, frameKey])

  const handleLoad = (e: SyntheticEvent<HTMLIFrameElement>) => {
    if (posted) {
      // The blank page loads before the post lands: only the editor counts.
      try {
        if (e.currentTarget.contentWindow?.location.href === 'about:blank') return
      } catch { /* cross-origin: Collabora's own page */ }
    }
    onLoad?.()
  }

  return (
    <>
      {posted && (
        <form ref={formRef} action={url} method="post" target={name} style={{ display: 'none' }}>
          <input type="hidden" name="access_token" value={accessToken ?? ''} />
          <input type="hidden" name="access_token_ttl" value={String(accessTokenTtl ?? 0)} />
        </form>
      )}
      <iframe
        key={`${name}-${frameKey}`}
        ref={iframeRef}
        name={name}
        src={posted ? undefined : url}
        data-collabora-frame=""
        className={className}
        style={style}
        sandbox={COLLABORA_SANDBOX}
        allow="clipboard-read; clipboard-write; fullscreen"
        allowFullScreen
        onLoad={handleLoad}
      />
    </>
  )
}
