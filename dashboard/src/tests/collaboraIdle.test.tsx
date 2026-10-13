import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { act, render } from '@testing-library/react'
import { useRef } from 'react'

// Collabora's idle end (useCollaboraIdle): a view dimmed after the frame
// lost the focus is asked its state on the person's return and given a
// moment to resume by itself (a pointer over its overlay), and is loaded
// again with a fresh token when it does not (a server-side end, a token
// near its end, a page that no longer answers, a load that failed after
// the document had loaded). The host never posts Grab_Focus.

import {
  IDLE_ANSWER_TIMEOUT_MS, IDLE_RESUME_GRACE_MS, TOKEN_END_MARGIN_MS, useCollaboraIdle,
} from '@/hooks/useCollaboraIdle'

const frameWin = { postMessage: vi.fn() } as unknown as Window & { postMessage: ReturnType<typeof vi.fn> }

function Harness({ loadKey, token, shown, recover }: {
  loadKey: number; token?: string; shown?: boolean; recover: () => void
}) {
  const iframeRef = useRef<HTMLIFrameElement | null>({ contentWindow: frameWin } as unknown as HTMLIFrameElement)
  const hostRef = useRef<HTMLDivElement | null>(null)
  useCollaboraIdle({ iframeRef, hostRef, loadKey, token, shown, recover })
  return <div ref={hostRef} data-testid="host"><input data-testid="composer" /></div>
}

const jwtWithExp = (expSeconds: number) =>
  `h.${btoa(JSON.stringify({ exp: expSeconds })).replace(/=+$/, '')}.s`

const fromFrame = (data: object, source: unknown = frameWin) => act(() => {
  window.dispatchEvent(new MessageEvent('message', { data: JSON.stringify(data), source: source as Window }))
})

const posted = () => frameWin.postMessage.mock.calls.map((c) => JSON.parse(String(c[0])).MessageId)

const returnTo = (host: HTMLElement) => act(() => {
  host.dispatchEvent(new Event('pointerover', { bubbles: true }))
})

function setup(props: { token?: string } = {}) {
  const recover = vi.fn()
  const utils = render(<Harness loadKey={0} recover={recover} {...props} />)
  const host = utils.getByTestId('host')
  return { ...utils, recover, host }
}

beforeEach(() => {
  vi.useFakeTimers()
  frameWin.postMessage.mockReset()
})
afterEach(() => {
  vi.useRealTimers()
})

describe('useCollaboraIdle', () => {
  it('does nothing on a return while the editor is active', () => {
    const { host, recover } = setup()
    returnTo(host)
    expect(posted()).toEqual([])
    expect(recover).not.toHaveBeenCalled()
  })

  it('an editor that resumed by itself (a pointer over its overlay) is left as it is', async () => {
    const { host, recover } = setup()
    fromFrame({ MessageId: 'User_Idle' })
    returnTo(host)
    returnTo(host)
    expect(posted()).toEqual(['Get_User_State'])   // several signals, one resume
    fromFrame({ MessageId: 'Get_User_State_Resp', Values: { State: 'active' } })
    await act(async () => { await vi.runAllTimersAsync() })
    expect(recover).not.toHaveBeenCalled()
    returnTo(host)
    expect(posted()).toEqual(['Get_User_State'])   // noted active: no new question
  })

  it('an idle editor gets a moment to resume and is asked again; resumed, it stays', async () => {
    const { host, recover } = setup()
    fromFrame({ MessageId: 'User_Idle' })
    returnTo(host)
    fromFrame({ MessageId: 'Get_User_State_Resp', Values: { State: 'idle' } })
    await act(async () => { await Promise.resolve() })
    expect(posted()).toEqual(['Get_User_State'])
    await act(async () => { await vi.advanceTimersByTimeAsync(IDLE_RESUME_GRACE_MS) })
    expect(posted()).toEqual(['Get_User_State', 'Get_User_State'])
    fromFrame({ MessageId: 'Get_User_State_Resp', Values: { State: 'active' } })
    await act(async () => { await vi.runAllTimersAsync() })
    expect(recover).not.toHaveBeenCalled()
  })

  it('still idle after the moment recovers once', async () => {
    const { host, recover } = setup()
    fromFrame({ MessageId: 'User_Idle' })
    returnTo(host)
    fromFrame({ MessageId: 'Get_User_State_Resp', Values: { State: 'idle' } })
    await act(async () => { await vi.advanceTimersByTimeAsync(IDLE_RESUME_GRACE_MS) })
    fromFrame({ MessageId: 'Get_User_State_Resp', Values: { State: 'idle' } })
    await act(async () => { await Promise.resolve() })
    expect(recover).toHaveBeenCalledTimes(1)
    returnTo(host)
    expect(recover).toHaveBeenCalledTimes(1)
  })

  it('an editor whose page no longer answers recovers', async () => {
    const { host, recover } = setup()
    fromFrame({ MessageId: 'User_Idle' })
    returnTo(host)
    await act(async () => { await vi.advanceTimersByTimeAsync(IDLE_ANSWER_TIMEOUT_MS + 10) })
    expect(recover).toHaveBeenCalledTimes(1)
  })

  it('a server-side end recovers at the first return, without asking', async () => {
    const { host, recover } = setup()
    fromFrame({ MessageId: 'Session_Closed', Values: { Reason: 'DocumentIdle' } })
    returnTo(host)
    expect(posted()).toEqual([])
    expect(recover).toHaveBeenCalledTimes(1)
  })

  it('a token near its end is not kept by an in-place resume', () => {
    const soon = Math.floor((Date.now() + TOKEN_END_MARGIN_MS / 2) / 1000)
    const { host, recover } = setup({ token: jwtWithExp(soon) })
    fromFrame({ MessageId: 'User_Idle' })
    returnTo(host)
    expect(posted()).toEqual([])
    expect(recover).toHaveBeenCalledTimes(1)
  })

  it('a load cancels a resume in flight', async () => {
    const recover = vi.fn()
    const { getByTestId, rerender } = render(<Harness loadKey={0} recover={recover} />)
    fromFrame({ MessageId: 'User_Idle' })
    returnTo(getByTestId('host'))
    rerender(<Harness loadKey={1} recover={recover} />)
    await act(async () => { await vi.runAllTimersAsync() })
    expect(recover).not.toHaveBeenCalled()
    returnTo(getByTestId('host'))
    expect(posted()).toEqual(['Get_User_State'])   // the new load starts active
  })

  it("another window's messages are not the editor's", () => {
    const { host, recover } = setup()
    fromFrame({ MessageId: 'Session_Closed' }, window)
    returnTo(host)
    expect(recover).not.toHaveBeenCalled()
  })

  it('a load that fails after the document had loaded recovers once; before, it does not', () => {
    const { recover } = setup()
    fromFrame({ MessageId: 'Action_Load_Resp', Values: { success: false } })
    expect(recover).not.toHaveBeenCalled()
    fromFrame({ MessageId: 'App_LoadingStatus', Values: { Status: 'Document_Loaded' } })
    fromFrame({ MessageId: 'Action_Load_Resp', Values: { success: false } })
    fromFrame({ MessageId: 'App_LoadingStatus', Values: { Status: 'Failed' } })
    expect(recover).toHaveBeenCalledTimes(1)
  })

  it('a return never posts Grab_Focus and the composer keeps the keyboard', async () => {
    const { getByTestId, host } = setup()
    fromFrame({ MessageId: 'User_Idle' })
    act(() => getByTestId('composer').focus())
    returnTo(host)
    await act(async () => { await vi.runAllTimersAsync() })
    expect(posted()).not.toContain('Grab_Focus')
    expect(document.activeElement).toBe(getByTestId('composer'))
  })

  it('closing the pane mid-resume recovers nothing', async () => {
    const recover = vi.fn()
    const { getByTestId, unmount } = render(<Harness loadKey={0} recover={recover} />)
    fromFrame({ MessageId: 'User_Idle' })
    returnTo(getByTestId('host'))
    unmount()
    await act(async () => { await vi.runAllTimersAsync() })
    expect(recover).not.toHaveBeenCalled()
  })

  it('a server-side end recovers when the pane is shown again or the focus moves in', () => {
    const recover = vi.fn()
    const { rerender } = render(<Harness loadKey={0} shown={false} recover={recover} />)
    fromFrame({ MessageId: 'Session_Closed', Values: { Reason: 'DocumentIdle' } })
    rerender(<Harness loadKey={0} shown recover={recover} />)
    expect(recover).toHaveBeenCalledTimes(1)

    const other = vi.fn()
    const { getAllByTestId } = render(<Harness loadKey={0} recover={other} />)
    act(() => getAllByTestId('composer')[1].focus())
    expect(other).not.toHaveBeenCalled()     // noted active: nothing to do
    expect(posted()).toEqual([])
  })

  it('the page visible again asks an idle editor too', () => {
    setup()
    fromFrame({ MessageId: 'User_Idle' })
    act(() => { document.dispatchEvent(new Event('visibilitychange')) })
    expect(posted()).toEqual(['Get_User_State'])
  })

  it("Collabora's own successful reload after a server-side end clears it", () => {
    const { host, recover } = setup()
    fromFrame({ MessageId: 'Session_Closed', Values: { Reason: 'DocumentIdle' } })
    fromFrame({ MessageId: 'User_Active' })
    returnTo(host)
    expect(recover).not.toHaveBeenCalled()
    fromFrame({ MessageId: 'Session_Closed', Values: { Reason: 'DocumentIdle' } })
    fromFrame({ MessageId: 'App_LoadingStatus', Values: { Status: 'Document_Loaded' } })
    returnTo(host)
    expect(recover).not.toHaveBeenCalled()
  })

  it('the pane shown again asks an idle editor', () => {
    const recover = vi.fn()
    const { rerender } = render(<Harness loadKey={0} shown={false} recover={recover} />)
    fromFrame({ MessageId: 'User_Idle' })
    rerender(<Harness loadKey={0} shown recover={recover} />)
    expect(posted()).toEqual(['Get_User_State'])
  })
})
