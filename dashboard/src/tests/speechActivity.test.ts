// speechActivity — the turn-end deferral contract. The synchronous-when-idle
// guarantee is load-bearing: appFrame.test asserts a file_updated reload
// lands in the same tick, and every deferred callsite (refetchChats,
// chat-pins, defensive refetch) relies on unchanged semantics when nothing
// is speaking.
import { describe, it, expect, vi, afterEach } from 'vitest'
import {
  isSpeaking, onIdle, setSpeaking, _resetSpeechActivity,
} from '../audio/speechActivity'

describe('speechActivity', () => {
  afterEach(() => {
    _resetSpeechActivity()
    vi.useRealTimers()
  })

  it('runs synchronously when nothing is speaking', () => {
    const fn = vi.fn()
    onIdle(fn)
    expect(fn).toHaveBeenCalledTimes(1)
  })

  it('queues while speaking and flushes on the idle edge, in order', () => {
    const calls: string[] = []
    setSpeaking(true)
    expect(isSpeaking()).toBe(true)
    onIdle(() => calls.push('a'))
    onIdle(() => calls.push('b'))
    expect(calls).toEqual([])
    setSpeaking(false)
    expect(isSpeaking()).toBe(false)
    expect(calls).toEqual(['a', 'b'])
  })

  it('is counted — idle means zero active sources', () => {
    const fn = vi.fn()
    setSpeaking(true) // duplex
    setSpeaking(true) // overlapping one-shot replay
    onIdle(fn)
    setSpeaking(false)
    expect(fn).not.toHaveBeenCalled() // one source still live
    setSpeaking(false)
    expect(fn).toHaveBeenCalledTimes(1)
  })

  it('the cap fires a wedged deferral exactly once', () => {
    vi.useFakeTimers()
    const fn = vi.fn()
    setSpeaking(true)
    onIdle(fn, 1000)
    vi.advanceTimersByTime(999)
    expect(fn).not.toHaveBeenCalled()
    vi.advanceTimersByTime(2)
    expect(fn).toHaveBeenCalledTimes(1)
    // The idle edge must not re-run a cap-fired entry.
    setSpeaking(false)
    expect(fn).toHaveBeenCalledTimes(1)
  })

  it('an extra release never goes negative', () => {
    setSpeaking(false)
    setSpeaking(true)
    const fn = vi.fn()
    onIdle(fn)
    expect(fn).not.toHaveBeenCalled()
    setSpeaking(false)
    expect(fn).toHaveBeenCalledTimes(1)
  })
})
