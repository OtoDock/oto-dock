import { useCallback, useEffect, useRef, type RefObject, useLayoutEffect } from 'react'

/** How long an idle editor gets to resume by itself (a pointer crossing
 * its overlay) before it is asked once more. */
export const IDLE_RESUME_GRACE_MS = 3000
/** How long the editor gets to answer `Get_User_State`: no answer means
 * its page no longer runs. */
export const IDLE_ANSWER_TIMEOUT_MS = 2000
/** A token this close to its end is not kept by an in-place resume. */
export const TOKEN_END_MARGIN_MS = 5 * 60 * 1000

/** The `exp` claim of a WOPI token (a JWT), in ms; null when unreadable.
 * Read for the timing only: the WOPI host judges the token. */
export const tokenEndMs = (token: string | null | undefined): number | null => {
  try {
    const part = (token ?? '').split('.')[1] ?? ''
    const exp = JSON.parse(atob(part.replace(/-/g, '+').replace(/_/g, '/')))?.exp
    return typeof exp === 'number' ? exp * 1000 : null
  } catch {
    return null
  }
}

const parse = (data: unknown): { MessageId?: string; Values?: Record<string, unknown> } | null => {
  try {
    const d = typeof data === 'string' ? JSON.parse(data) : data
    return d && typeof d === 'object' ? d as { MessageId?: string; Values?: Record<string, unknown> } : null
  } catch {
    return null
  }
}

const post = (win: Window, MessageId: string, Values: object = {}) => {
  try {
    win.postMessage(JSON.stringify({ MessageId, Values }), '*')
    return true
  } catch {
    return false
  }
}

/** Ask Collabora to save the document's unsaved edits and wait for its
 * answer (`Action_Save_Resp`), at most `timeoutMs`: true on a confirmed
 * save, false otherwise. Collabora confirms its own save whatever the
 * WOPI upload answered, so the caller judges "the file changed under the
 * edits" itself. */
export function postSaveAndWait(win: Window, timeoutMs: number): Promise<boolean> {
  return new Promise<boolean>((resolve) => {
    const done = (v: boolean) => {
      window.removeEventListener('message', onMsg)
      clearTimeout(timer)
      resolve(v)
    }
    const onMsg = (e: MessageEvent) => {
      if (e.source !== win) return
      const d = parse(e.data)
      if (d?.MessageId === 'Action_Save_Resp') done(d.Values?.success === true)
    }
    const timer = setTimeout(() => done(false), timeoutMs)
    window.addEventListener('message', onMsg)
    if (!post(win, 'Action_Save', { DontTerminateEdit: true, DontSaveIfUnmodified: true, Notify: true })) {
      done(false)
    }
  })
}

interface Args {
  iframeRef: RefObject<HTMLIFrameElement | null>
  /** The element whose pointer and press events are the person's return
   * (the pane's or the preview's box; the frame's own `pointerover`
   * reaches it). */
  hostRef: RefObject<HTMLElement | null>
  /** Moves with every load of the frame: a load ends what the previous
   * one noted. */
  loadKey: unknown
  /** The token the frame was posted (its end decides whether an in-place
   * resume may keep it). */
  token?: string | null
  /** The frame is on show (a pane put away is not): its return to show
   * is the person's return too. */
  shown?: boolean
  /** Load the document again with a fresh token. */
  recover: () => void
}

type Idle = 'active' | 'idle' | 'closed'

/**
 * Bring an editor back from Collabora's idle end in place.
 *
 * Collabora dims a view 5 minutes after its frame lost the focus (or 15
 * without input) and posts `User_Idle`; its socket stays up, and a pointer
 * crossing its overlay resumes it. It cannot resume by itself when the
 * document must be loaded again (a server-side end, `Session_Closed`, or a
 * socket that dropped) with a token past its end or a sign-in that ended,
 * and nothing crosses the overlay when the person comes back another way
 * (a pane restored, a press on the pane, the keyboard). `Grab_Focus` is no
 * help on 25.04: it marks the view active again but leaves the overlay up
 * (its `_activate` closes a dialog, the overlay is an info modal), and it
 * takes the keyboard from wherever the person types.
 *
 * On the person's return (a pointer entering the host, a press or the
 * focus moving into it, the frame shown again, the page visible again)
 * while the frame is noted idle: after `Session_Closed`, or with the token
 * near its end, `recover` at once; else ask `Get_User_State`, and when it
 * answers idle give the editor `IDLE_RESUME_GRACE_MS` to resume by itself
 * and ask again: `recover` when it still answers idle or does not answer.
 * A load that fails after the document had loaded (a reconnect refused)
 * recovers too, once per load.
 */
export function useCollaboraIdle({ iframeRef, hostRef, loadKey, token, shown = true, recover }: Args) {
  const state = useRef<Idle>('active')
  const loaded = useRef(false)
  const recovered = useRef(false)
  const resuming = useRef(false)
  const timers = useRef<number[]>([])
  const answer = useRef<((s: string | null) => void) | null>(null)
  const recoverRef = useRef(recover)
  const tokenRef = useRef(token)
  useLayoutEffect(() => {
    recoverRef.current = recover
    tokenRef.current = token
  })

  const clearTimers = useCallback(() => {
    for (const t of timers.current) clearTimeout(t)
    timers.current = []
    answer.current?.(null)
    answer.current = null
  }, [])

  // Every load starts active, with nothing pending.
  useEffect(() => {
    state.current = 'active'
    loaded.current = false
    recovered.current = false
    resuming.current = false
    clearTimers()
  }, [loadKey, clearTimers])

  useEffect(() => () => {
    resuming.current = false
    recovered.current = true
    clearTimers()
  }, [clearTimers])

  const doRecover = useCallback(() => {
    if (recovered.current) return
    recovered.current = true
    clearTimers()
    resuming.current = false
    recoverRef.current()
  }, [clearTimers])

  const ask = useCallback((win: Window): Promise<string | null> => new Promise((resolve) => {
    answer.current?.(null)
    const t = window.setTimeout(() => {
      if (answer.current === settle) answer.current = null
      resolve(null)
    }, IDLE_ANSWER_TIMEOUT_MS)
    timers.current.push(t)
    const settle = (s: string | null) => {
      clearTimeout(t)
      resolve(s)
    }
    answer.current = settle
    if (!post(win, 'Get_User_State')) settle(null)
  }), [])

  const resume = useCallback(async () => {
    if (resuming.current || recovered.current || state.current === 'active') return
    const win = iframeRef.current?.contentWindow
    if (!win) return
    const end = tokenEndMs(tokenRef.current)
    if (state.current === 'closed' || (end !== null && end - Date.now() < TOKEN_END_MARGIN_MS)) {
      doRecover()
      return
    }
    resuming.current = true
    const first = await ask(win)
    if (!resuming.current) return
    if (first === 'active') {
      state.current = 'active'
      resuming.current = false
      return
    }
    if (first === 'idle') {
      await new Promise<void>((r) => { timers.current.push(window.setTimeout(r, IDLE_RESUME_GRACE_MS)) })
      if (!resuming.current) return
      const second = await ask(win)
      if (!resuming.current) return
      if (second === 'active') {
        state.current = 'active'
        resuming.current = false
        return
      }
    }
    doRecover()
  }, [iframeRef, ask, doRecover])

  // The editor's status: idle, closed, active again, loaded, a load that
  // failed after it had loaded, the answers to Get_User_State.
  useEffect(() => {
    const onMsg = (e: MessageEvent) => {
      if (!iframeRef.current || e.source !== iframeRef.current.contentWindow) return
      const d = parse(e.data)
      switch (d?.MessageId) {
        case 'User_Idle':
          if (state.current === 'active') state.current = 'idle'
          break
        case 'Session_Closed':
          state.current = 'closed'
          break
        case 'User_Active':
          // Collabora resumed by itself (a pointer over its overlay), a
          // server-side end included when its own reload succeeded.
          state.current = 'active'
          break
        case 'Get_User_State_Resp': {
          const settle = answer.current
          answer.current = null
          settle?.(typeof d.Values?.State === 'string' ? d.Values.State : null)
          break
        }
        case 'App_LoadingStatus':
          if (d.Values?.Status === 'Document_Loaded') {
            loaded.current = true
            state.current = 'active'
          } else if (d.Values?.Status === 'Failed' && loaded.current) doRecover()
          break
        case 'Action_Load_Resp':
          if (d.Values?.success === false && loaded.current) doRecover()
          break
      }
    }
    window.addEventListener('message', onMsg)
    return () => window.removeEventListener('message', onMsg)
  }, [iframeRef, doRecover])

  // The person's return: a pointer entering the host (the frame's own
  // pointerover included), a press or the focus moving into it, the page
  // visible again.
  useEffect(() => {
    const host = hostRef.current
    if (!host) return
    const onReturn = () => { void resume() }
    const onVisible = () => { if (document.visibilityState === 'visible') onReturn() }
    host.addEventListener('pointerover', onReturn)
    host.addEventListener('pointerdown', onReturn, { capture: true })
    host.addEventListener('focusin', onReturn)
    document.addEventListener('visibilitychange', onVisible)
    return () => {
      host.removeEventListener('pointerover', onReturn)
      host.removeEventListener('pointerdown', onReturn, { capture: true })
      host.removeEventListener('focusin', onReturn)
      document.removeEventListener('visibilitychange', onVisible)
    }
  }, [hostRef, resume])

  // The frame shown again (a pane restored from Minimize or an overlay).
  const wasShown = useRef(shown)
  useEffect(() => {
    if (shown && !wasShown.current) void resume()
    wasShown.current = shown
  }, [shown, resume])
}
