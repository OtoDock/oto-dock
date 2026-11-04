import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { renderHook } from '@testing-library/react'
import { installFakeNativeChannel, type FakeNativeChannel } from './fixtures/nativeChannel'
import { emitAppToast } from '@/lib/appToasts'
import { NOTIFICATION_SEVERITY } from '@/lib/status/notificationSeverity'
import { STORE_REVIEW_TIMEOUT_MS } from '@/lib/nativeBridge'
import { acquireMic, releaseMic } from '@/audio/micCoordinator'
import { setSpeaking } from '@/audio/speechActivity'
import {
  storeReviewKey, localDay, mergeRecords, noteChatAtBottom, noteStoreReviewError, noteStoreReviewMoment,
  noteTurnEnded, noteTurnSent, noteTurnStreaming, parseRecord, setStoreReviewSettings, startStoreReview,
  type StoreReviewRecord,
} from '@/lib/device/storeReview'
import { useStoreReview, useStoreReviewTurns, type FrameSubscribe } from '@/hooks/storeReview/useStoreReview'
import { WIRE, type WireFrame } from '@/api/wireEvents'
import { useChatStore } from '@/store/chatStore'
import { CHAT_PHASE } from '@/lib/status/chat'

// ─── When the apps ask for a store review: only in an app shell, never on
//     the first day or in a session's first three minutes, after a success
//     moment, never right after an error, twice at most, the record kept on
//     the device and on the server. ─────────────────────────────────────────

const h = vi.hoisted(() => ({
  native: true,
  server: undefined as unknown,
  readFails: false,
  flagOn: true,
  calls: [] as { method: string; body?: unknown }[],
  logs: [] as string[],
}))

vi.mock('@capacitor/core', () => ({ Capacitor: { isNativePlatform: () => h.native, getPlatform: () => 'android' } }))
vi.mock('@/lib/debug/log', () => ({
  debugLog: (...a: unknown[]) => { h.logs.push(a.map(String).join(' ')) },
  debugWarn: () => {}, logError: () => {}, isDebugOn: () => true,
}))
vi.mock('@/lib/http/apiJson', () => ({
  apiJson: async (_path: string, init?: { method?: string; json?: { store_review?: unknown } }) => {
    if (init?.method === 'PUT') {
      h.calls.push({ method: 'PUT', body: init.json?.store_review })
      h.server = init.json?.store_review
      return { store_review: h.server, other: 1 }
    }
    h.calls.push({ method: 'GET' })
    if (h.readFails) throw new Error('offline')
    return h.server === undefined ? { other: 1 } : { store_review: h.server, other: 1 }
  },
}))
vi.mock('@/hooks/useFlag', () => ({ useFlag: (name: string) => (name === 'store_review_days' ? 90 : h.flagOn) }))

const MIN = 60 * 1000
const DAY = 24 * 60 * MIN
const T0 = new Date(2026, 9, 29, 10, 0, 0).getTime()

let ch: FakeNativeChannel
let stop: (() => void) | null = null
let hidden = false

const reviewPosts = () => ch.posted.filter((p) => p.m === 'requestStoreReview').length
const flush = () => vi.advanceTimersByTimeAsync(0)
const SUB = 'person-1'
const device = (sub = SUB): StoreReviewRecord | null => parseRecord(JSON.parse(localStorage.getItem(storeReviewKey(sub)) || 'null'))
const seed = (r: StoreReviewRecord, where: 'device' | 'server' = 'device') => {
  if (where === 'device') localStorage.setItem(storeReviewKey(SUB), JSON.stringify(r))
  else h.server = r
}
/** A record that began three days ago, opened on each (so today adds no day). */
const older = (): StoreReviewRecord => ({
  since: T0 - 3 * DAY, days: [localDay(T0 - 3 * DAY), localDay(T0 - 2 * DAY), localDay(T0 - DAY)], asked: [],
})

async function boot() {
  stop = startStoreReview(SUB)
  await flush()
}

/** A clean turn read to its end in chat c1. */
function cleanReply(chat = 'c1') {
  noteTurnSent(chat)
  noteChatAtBottom(chat, true)
  noteTurnStreaming(chat)
  noteTurnEnded(chat, true)
}

beforeEach(() => {
  vi.useFakeTimers()
  vi.setSystemTime(T0)
  h.native = true
  h.server = undefined
  h.readFails = false
  h.flagOn = true
  h.calls = []
  h.logs = []
  hidden = false
  Object.defineProperty(document, 'hidden', { configurable: true, get: () => hidden })
  localStorage.clear()
  setStoreReviewSettings(true, 90)
  ch = installFakeNativeChannel({ requestStoreReview: true })
})

afterEach(() => {
  stop?.()
  stop = null
  ch.uninstall()
  vi.useRealTimers()
})

describe('the record', () => {
  it('reads only well-formed records and merges them as a union', () => {
    expect(parseRecord({ since: 5, days: ['2026-10-01'], asked: [] })).toEqual({ since: 5, days: ['2026-10-01'], asked: [] })
    for (const bad of [null, 'x', { since: 0, days: [], asked: [] }, { since: 5, days: ['01/10'], asked: [] }, { since: 5, days: [], asked: ['1'] }]) {
      expect(parseRecord(bad)).toBeNull()
    }
    const a = { since: 10, days: ['2026-10-02', '2026-10-01'], asked: [30] }
    const b = { since: 5, days: ['2026-10-03', '2026-10-04'], asked: [20, 30, 40] }
    expect(mergeRecords(a, b)).toEqual({ since: 5, days: ['2026-10-01', '2026-10-02', '2026-10-03'], asked: [20, 30] })
  })
})

describe('the store review ask', () => {
  it('reads and writes nothing and never asks in a browser', async () => {
    h.native = false
    renderHook(() => useStoreReview(SUB))
    await flush()
    cleanReply()
    await flush()
    expect(h.calls).toEqual([])
    expect(reviewPosts()).toBe(0)
  })

  it('never asks on the first day; the record starts on both sides', async () => {
    await boot()
    vi.advanceTimersByTime(5 * MIN)
    cleanReply()
    await flush()
    expect(reviewPosts()).toBe(0)
    expect(h.logs.some((l) => l.includes('moment reply, not asked: the first day'))).toBe(true)
    expect(device()).toEqual({ since: T0, days: [localDay(T0)], asked: [] })
    expect(h.server).toEqual({ since: T0, days: [localDay(T0)], asked: [] })
  })

  it('asks after a clean reply read to its end, and records the ask on both sides', async () => {
    seed(older())
    await boot()
    await vi.advanceTimersByTimeAsync(5 * MIN)
    cleanReply()
    await flush()
    expect(reviewPosts()).toBe(1)
    expect(ch.posted.find((p) => p.m === 'requestStoreReview')?.a).toEqual([])
    const at = T0 + 5 * MIN
    expect(device()?.asked).toEqual([at])
    expect((h.server as StoreReviewRecord).asked).toEqual([at])
    expect(h.logs.some((l) => l.includes('handed to the store'))).toBe(true)
  })

  it('a turn with an error or a stop is no moment', async () => {
    seed(older())
    await boot()
    await vi.advanceTimersByTimeAsync(5 * MIN)
    noteChatAtBottom('c1', true)
    noteTurnSent('c1')
    noteTurnEnded('c1', false)
    await flush()
    expect(reviewPosts()).toBe(0)
  })

  it('a clean reply away from the end waits for the end, ten minutes at most', async () => {
    seed(older())
    await boot()
    await vi.advanceTimersByTimeAsync(5 * MIN)
    noteChatAtBottom('c1', false)
    noteTurnSent('c1')
    noteTurnEnded('c1', true)
    await flush()
    expect(reviewPosts()).toBe(0)
    noteChatAtBottom('c2', true)
    await flush()
    expect(reviewPosts()).toBe(0)
    noteChatAtBottom('c1', true)
    await flush()
    expect(reviewPosts()).toBe(1)
  })

  it('a reply reached after ten minutes, or after a new turn began, counts nothing', async () => {
    seed(older())
    await boot()
    await vi.advanceTimersByTimeAsync(5 * MIN)
    noteChatAtBottom('c1', false)
    noteTurnSent('c1')
    noteTurnEnded('c1', true)
    await vi.advanceTimersByTimeAsync(11 * MIN)
    noteChatAtBottom('c1', true)
    await flush()
    noteChatAtBottom('c1', false)
    noteTurnSent('c1')
    noteTurnEnded('c1', true)
    noteTurnStreaming('c1')
    noteChatAtBottom('c1', true)
    await flush()
    expect(reviewPosts()).toBe(0)
  })

  it('a clean turn on a chat this page never sent to counts nothing', async () => {
    seed(older())
    await boot()
    await vi.advanceTimersByTimeAsync(5 * MIN)
    noteChatAtBottom('shared', true)
    noteTurnStreaming('shared')
    noteTurnEnded('shared', true)
    await flush()
    expect(reviewPosts()).toBe(0)
    expect(h.logs.some((l) => l.includes('a turn ended that this page did not send'))).toBe(true)
    noteTurnSent('shared')
    noteTurnEnded('shared', true)
    await flush()
    expect(reviewPosts()).toBe(1)
  })

  it('a send that opens a new chat counts for the chat the server names', async () => {
    seed(older())
    await boot()
    await vi.advanceTimersByTimeAsync(5 * MIN)
    noteTurnSent(null)
    noteChatAtBottom('fresh', true)
    noteTurnStreaming('fresh')
    noteTurnEnded('fresh', true)
    await flush()
    expect(reviewPosts()).toBe(1)
  })

  it('a moment in the first three minutes is held to the mark', async () => {
    seed(older())
    await boot()
    await vi.advanceTimersByTimeAsync(MIN)
    cleanReply()
    await flush()
    expect(reviewPosts()).toBe(0)
    expect(h.logs.some((l) => l.includes("held until the session's third minute"))).toBe(true)
    await vi.advanceTimersByTimeAsync(2 * MIN)
    expect(reviewPosts()).toBe(1)
  })

  it('a held moment is dropped by an error before the mark, or a hidden page at it', async () => {
    seed(older())
    await boot()
    noteStoreReviewMoment('push')
    await vi.advanceTimersByTimeAsync(2 * MIN)
    noteStoreReviewError()
    await vi.advanceTimersByTimeAsync(2 * MIN)
    expect(reviewPosts()).toBe(0)
    stop?.()
    stop = startStoreReview(SUB)
    await flush()
    noteStoreReviewMoment('push')
    hidden = true
    await vi.advanceTimersByTimeAsync(4 * MIN)
    expect(reviewPosts()).toBe(0)
  })

  it('an error toast or a refused card holds every ask for two minutes', async () => {
    seed(older())
    await boot()
    await vi.advanceTimersByTimeAsync(5 * MIN)
    emitAppToast({ title: 'The setting was not saved', body: '', severity: NOTIFICATION_SEVERITY.WARNING })
    cleanReply()
    await flush()
    expect(reviewPosts()).toBe(0)
    emitAppToast({ title: 'Saved', body: '', severity: NOTIFICATION_SEVERITY.SUCCESS })
    await vi.advanceTimersByTimeAsync(2 * MIN)
    noteStoreReviewError()
    cleanReply()
    await flush()
    expect(reviewPosts()).toBe(0)
    await vi.advanceTimersByTimeAsync(2 * MIN)
    cleanReply()
    await flush()
    expect(reviewPosts()).toBe(1)
  })

  it('the third distinct day the app is opened is a moment, held to the mark', async () => {
    seed({ since: T0 - 2 * DAY, days: [localDay(T0 - 2 * DAY), localDay(T0 - DAY)], asked: [] }, 'server')
    await boot()
    expect(h.logs.some((l) => l.includes('moment day'))).toBe(true)
    expect(reviewPosts()).toBe(0)
    await vi.advanceTimersByTimeAsync(3 * MIN)
    expect(reviewPosts()).toBe(1)
    expect((h.server as StoreReviewRecord).days).toHaveLength(3)
  })

  it('the second distinct day is no moment', async () => {
    seed({ since: T0 - DAY, days: [localDay(T0 - DAY)], asked: [] })
    await boot()
    await vi.advanceTimersByTimeAsync(5 * MIN)
    expect(reviewPosts()).toBe(0)
    expect(device()?.days).toEqual([localDay(T0 - DAY), localDay(T0)])
  })

  it('asks a second time only after the admin days, and never a third', async () => {
    setStoreReviewSettings(true, 30)
    seed({ ...older(), asked: [T0 - 29 * DAY] })
    await boot()
    await vi.advanceTimersByTimeAsync(5 * MIN)
    cleanReply()
    await flush()
    expect(reviewPosts()).toBe(0)
    expect(h.logs.some((l) => l.includes('asked once, waits 30 days'))).toBe(true)
    await vi.advanceTimersByTimeAsync(DAY)
    cleanReply()
    await flush()
    expect(reviewPosts()).toBe(1)
    expect(device()?.asked).toHaveLength(2)
    await vi.advanceTimersByTimeAsync(400 * DAY)
    cleanReply()
    await flush()
    expect(reviewPosts()).toBe(1)
    expect(h.logs.some((l) => l.includes('asked twice already'))).toBe(true)
  })

  it('records nothing and stops for the page when the app does not hand it over', async () => {
    ch.uninstall()
    ch = installFakeNativeChannel({ requestStoreReview: false })
    seed(older())
    await boot()
    await vi.advanceTimersByTimeAsync(5 * MIN)
    cleanReply()
    await flush()
    cleanReply()
    await flush()
    expect(reviewPosts()).toBe(1)
    expect(device()?.asked).toEqual([])
  })

  it('an app that never answers (older, or iOS before the case) reads not handed after the wait', async () => {
    ch.uninstall()
    ch = installFakeNativeChannel()
    seed(older())
    await boot()
    await vi.advanceTimersByTimeAsync(5 * MIN)
    cleanReply()
    await vi.advanceTimersByTimeAsync(STORE_REVIEW_TIMEOUT_MS)
    expect(device()?.asked).toEqual([])
    expect(h.logs.some((l) => l.includes('not handed to a store'))).toBe(true)
  })

  it('a reinstall finds the two asks on the server and never asks again', async () => {
    seed({ ...older(), asked: [T0 - 400 * DAY, T0 - 200 * DAY] }, 'server')
    await boot()
    await vi.advanceTimersByTimeAsync(5 * MIN)
    cleanReply()
    await flush()
    expect(reviewPosts()).toBe(0)
    expect(device()?.asked).toHaveLength(2)
  })

  it('another person on the same phone and server keeps their own device copy', async () => {
    localStorage.setItem(storeReviewKey('person-2'), JSON.stringify({ ...older(), asked: [T0 - 200 * DAY, T0 - 100 * DAY] }))
    await boot()
    expect(device()?.asked).toEqual([])
    expect(device()?.since).toBe(T0)
    expect((h.server as StoreReviewRecord).asked).toEqual([])
    expect(device('person-2')?.asked).toHaveLength(2)
  })

  it('another phone that asked since the start counts before this one asks', async () => {
    seed(older(), 'server')
    seed(older())
    await boot()
    await vi.advanceTimersByTimeAsync(5 * MIN)
    h.server = { ...older(), days: [...older().days, localDay(T0)], asked: [T0 + MIN] }
    cleanReply()
    await flush()
    expect(reviewPosts()).toBe(0)
    expect(h.logs.some((l) => l.includes('not asked after reading the server: asked once'))).toBe(true)
  })

  it('an ask the server did not hear of stays on the device and reaches the server at the next start', async () => {
    ch.uninstall()
    ch = installFakeNativeChannel()
    seed(older())
    await boot()
    await vi.advanceTimersByTimeAsync(5 * MIN)
    cleanReply()
    await flush()
    const post = ch.posted.find((p) => p.m === 'requestStoreReview')
    expect(post).toBeDefined()
    h.readFails = true
    ch.reply(post!.id!, true)
    await flush()
    const at = T0 + 5 * MIN
    expect(device()?.asked).toEqual([at])
    expect((h.server as StoreReviewRecord | undefined)?.asked ?? []).toEqual([])
    cleanReply()
    await flush()
    expect(reviewPosts()).toBe(1)
    stop?.()
    h.readFails = false
    await boot()
    expect((h.server as StoreReviewRecord).asked).toEqual([at])
  })

  it('a moment whose read of the server fails asks nothing', async () => {
    seed(older())
    await boot()
    await vi.advanceTimersByTimeAsync(5 * MIN)
    h.readFails = true
    cleanReply()
    await flush()
    expect(reviewPosts()).toBe(0)
    expect(h.logs.some((l) => l.includes('not asked: the server copy was not read'))).toBe(true)
  })

  it('without the server copy nothing is asked and no day is added', async () => {
    h.readFails = true
    seed(older())
    await boot()
    await vi.advanceTimersByTimeAsync(5 * MIN)
    cleanReply()
    await flush()
    expect(reviewPosts()).toBe(0)
    expect(device()?.days).toEqual(older().days)
  })

  it('reads the server once per session, and only for a day it lacks', async () => {
    seed({ since: T0 - DAY, days: [localDay(T0 - DAY)], asked: [] })
    await boot()
    const gets = () => h.calls.filter((c) => c.method === 'GET').length
    const show = async (away: number) => {
      hidden = true
      document.dispatchEvent(new Event('visibilitychange'))
      await vi.advanceTimersByTimeAsync(away)
      hidden = false
      document.dispatchEvent(new Event('visibilitychange'))
      await flush()
    }
    const first = gets()
    expect(device()?.days).toHaveLength(2)
    await show(10 * MIN)
    expect(gets()).toBe(first)
    await show(DAY)
    expect(gets()).toBe(first + 1)
    expect(device()?.days).toHaveLength(3)
    expect(h.logs.some((l) => l.includes('moment day'))).toBe(true)
    await show(DAY)
    expect(gets()).toBe(first + 1)
  })

  it('turned off by the admin, the record is neither read nor written', async () => {
    h.flagOn = false
    seed(older())
    const view = renderHook(() => useStoreReview(SUB))
    await vi.advanceTimersByTimeAsync(5 * MIN)
    cleanReply()
    noteStoreReviewMoment('push')
    await flush()
    expect(h.calls).toEqual([])
    expect(reviewPosts()).toBe(0)
    view.unmount()
  })

  it('turned off by the admin, nothing is asked', async () => {
    setStoreReviewSettings(false, 90)
    seed(older())
    await boot()
    await vi.advanceTimersByTimeAsync(5 * MIN)
    cleanReply()
    await flush()
    expect(reviewPosts()).toBe(0)
  })

  it('a held moment is dropped while a voice conversation or dictation holds the microphone', async () => {
    seed(older())
    await boot()
    noteStoreReviewMoment('push')
    acquireMic('duplex')
    await vi.advanceTimersByTimeAsync(3 * MIN)
    releaseMic('duplex')
    expect(reviewPosts()).toBe(0)
    expect(h.logs.some((l) => l.includes('a voice conversation or dictation is on'))).toBe(true)
  })

  it('never while a reply is read aloud', async () => {
    seed(older())
    await boot()
    await vi.advanceTimersByTimeAsync(5 * MIN)
    setSpeaking(true)
    noteStoreReviewMoment('push')
    await flush()
    setSpeaking(false)
    expect(reviewPosts()).toBe(0)
    expect(h.logs.some((l) => l.includes('a reply is being read aloud'))).toBe(true)
  })

  it('never while a reply streams in the chat on screen', async () => {
    seed(older())
    await boot()
    await vi.advanceTimersByTimeAsync(5 * MIN)
    useChatStore.getState().setStreaming('c1')
    expect(useChatStore.getState().byChat.c1?.status).toBe(CHAT_PHASE.STREAMING)
    noteChatAtBottom('c1', true)
    noteStoreReviewMoment('push')
    await flush()
    useChatStore.getState().setReady('c1')
    expect(reviewPosts()).toBe(0)
    expect(h.logs.some((l) => l.includes('a reply is streaming'))).toBe(true)
  })

  it('a return after thirty minutes away starts a new session; a shorter one does not', async () => {
    seed(older())
    await boot()
    await vi.advanceTimersByTimeAsync(5 * MIN)
    const away = async (ms: number) => {
      hidden = true
      document.dispatchEvent(new Event('visibilitychange'))
      await vi.advanceTimersByTimeAsync(ms)
      hidden = false
      document.dispatchEvent(new Event('visibilitychange'))
      await flush()
    }
    await away(31 * MIN)
    noteStoreReviewMoment('push')
    await flush()
    expect(reviewPosts()).toBe(0)
    expect(h.logs.some((l) => l.includes("held until the session's third minute"))).toBe(true)
    await vi.advanceTimersByTimeAsync(3 * MIN)
    expect(reviewPosts()).toBe(1)
  })

  it('a return within thirty minutes keeps the session', async () => {
    seed(older())
    await boot()
    await vi.advanceTimersByTimeAsync(5 * MIN)
    hidden = true
    document.dispatchEvent(new Event('visibilitychange'))
    await vi.advanceTimersByTimeAsync(20 * MIN)
    hidden = false
    document.dispatchEvent(new Event('visibilitychange'))
    noteStoreReviewMoment('push')
    await flush()
    expect(reviewPosts()).toBe(1)
  })

  it('a start or an ask dated in the future never unlocks an ask early', async () => {
    seed({ ...older(), since: T0 + 5 * DAY })
    await boot()
    await vi.advanceTimersByTimeAsync(5 * MIN)
    noteStoreReviewMoment('push')
    await flush()
    expect(reviewPosts()).toBe(0)
    expect(h.logs.some((l) => l.includes('not asked: the first day'))).toBe(true)
    stop?.()
    localStorage.clear()
    h.server = undefined
    seed({ ...older(), asked: [T0 + 5 * DAY] })
    await boot()
    await vi.advanceTimersByTimeAsync(5 * MIN)
    noteStoreReviewMoment('push')
    await flush()
    expect(reviewPosts()).toBe(0)
    expect(h.logs.some((l) => l.includes('asked once, waits 90 days'))).toBe(true)
  })

  it('signing out clears a held moment', async () => {
    seed(older())
    const view = renderHook(({ sub }: { sub: string | null }) => useStoreReview(sub), { initialProps: { sub: SUB as string | null } })
    await flush()
    noteStoreReviewMoment('push')
    expect(h.logs.some((l) => l.includes("held until the session's third minute"))).toBe(true)
    view.rerender({ sub: null })
    await vi.advanceTimersByTimeAsync(5 * MIN)
    expect(reviewPosts()).toBe(0)
    view.unmount()
  })

  it('never while the person is typing', async () => {
    seed(older())
    await boot()
    await vi.advanceTimersByTimeAsync(5 * MIN)
    const box = document.createElement('textarea')
    document.body.appendChild(box)
    box.focus()
    cleanReply()
    await flush()
    expect(reviewPosts()).toBe(0)
    expect(h.logs.some((l) => l.includes('the person is typing'))).toBe(true)
    box.remove()
  })
})

/** The chat page's socket registry, by frame type. */
function fakeSocket() {
  const subs = new Map<string, Set<(m: WireFrame) => void>>()
  const subscribe: FrameSubscribe = (type, fn) => {
    const set = subs.get(type) ?? new Set()
    set.add(fn)
    subs.set(type, set)
    return () => { set.delete(fn) }
  }
  const emit = (frame: Record<string, unknown>) => subs.get(frame.type as string)?.forEach((fn) => fn(frame as unknown as WireFrame))
  return { subscribe, emit, count: () => [...subs.values()].reduce((n, set) => n + set.size, 0) }
}

describe('the turn watch on the chat page', () => {
  async function ready() {
    seed(older())
    await boot()
    await vi.advanceTimersByTimeAsync(5 * MIN)
    noteChatAtBottom('c1', true)
    noteTurnSent('c1')
    const ws = fakeSocket()
    const view = renderHook(() => useStoreReviewTurns(ws.subscribe))
    return { ws, view }
  }

  it('a turn that streamed text and ended clean is the moment, once', async () => {
    const { ws } = await ready()
    ws.emit({ type: WIRE.TEXT, chat_id: 'c1', content: 'ready' })
    ws.emit({ type: WIRE.TEXT, chat_id: 'c1', content: '.' })
    ws.emit({ type: WIRE.DONE, chat_id: 'c1' })
    await flush()
    expect(reviewPosts()).toBe(1)
  })

  it('a done with no text before it, or of another chat, counts nothing', async () => {
    const { ws } = await ready()
    ws.emit({ type: WIRE.DONE, chat_id: 'c1' })
    ws.emit({ type: WIRE.TEXT, chat_id: 'c1', content: 'x' })
    ws.emit({ type: WIRE.DONE, chat_id: 'c2' })
    await flush()
    expect(reviewPosts()).toBe(0)
  })

  it('an error frame ends the turn unclean and starts the quiet window', async () => {
    const { ws } = await ready()
    ws.emit({ type: WIRE.TEXT, chat_id: 'c1', content: 'x' })
    ws.emit({ type: WIRE.ERROR, chat_id: 'c1', message: 'The turn ended early.', reason: 'limit' })
    ws.emit({ type: WIRE.DONE, chat_id: 'c1' })
    await flush()
    noteStoreReviewMoment('push')
    await flush()
    expect(reviewPosts()).toBe(0)
    expect(h.logs.some((l) => l.includes('an error came less than two minutes ago'))).toBe(true)
  })

  it('a refused queued message is no failed turn', async () => {
    const { ws } = await ready()
    ws.emit({ type: WIRE.TEXT, chat_id: 'c1', content: 'x' })
    ws.emit({ type: WIRE.ERROR, chat_id: 'c1', message: 'Refused.', queue_id: 'q1' })
    ws.emit({ type: WIRE.DONE, chat_id: 'c1' })
    await flush()
    expect(reviewPosts()).toBe(1)
  })

  it('a stopped turn counts nothing, and a failed warm-up starts the quiet window', async () => {
    const { ws } = await ready()
    ws.emit({ type: WIRE.TEXT, chat_id: 'c1', content: 'x' })
    ws.emit({ type: WIRE.ABORTED, chat_id: 'c1' })
    ws.emit({ type: WIRE.DONE, chat_id: 'c1' })
    await flush()
    expect(reviewPosts()).toBe(0)
    ws.emit({ type: WIRE.WARMUP_FAILED, chat_id: 'c1', error: 'no engine' })
    noteStoreReviewMoment('push')
    await flush()
    expect(reviewPosts()).toBe(0)
    expect(h.logs.some((l) => l.includes('an error came less than two minutes ago'))).toBe(true)
  })

  it('leaves the socket when the page goes', async () => {
    const { ws, view } = await ready()
    expect(ws.count()).toBe(5)
    view.unmount()
    expect(ws.count()).toBe(0)
  })
})
