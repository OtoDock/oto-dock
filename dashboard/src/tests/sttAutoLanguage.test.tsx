import { describe, it, expect, vi, beforeEach } from 'vitest'
import { renderHook, act } from '@testing-library/react'
import { useSpeechSession } from '@/hooks/useSpeechSession'

import { AUTO_LANGUAGE, isAutoLanguage } from '@/audio/lang'
import type { STTSession } from '@/audio/types'
import { currentSttLanguage, STT_MENU_OPTIONS } from '@/lib/chat/sttLanguages'

// ─── The Automatic dictation language ───────────────────────────────────────
// Stored as "auto": the platform engine gets it and identifies the language;
// the device recognizer needs a code and keeps the browser language.

const prefs = { stt_mode: 'auto', stt_language: 'auto' as string | null }
let backendKind: 'native' | 'platform' = 'platform'
const created: { language: string }[] = []

vi.mock('@/hooks/useChatAudioCapability', () => ({
  useChatAudioCapability: () => ({ data: { stt: 'either', stt_provider_id: 1 } }),
}))
vi.mock('@/api/userAudio', () => ({
  useMyAudioPrefs: () => ({ data: prefs }),
}))
vi.mock('@/audio/backends/platformStt', () => ({
  platformStt: { kind: 'platform', isAvailable: () => false, create: () => { throw new Error('unused') } },
  STOP_FINAL_WAIT_MS: 10000,
  STOP_BACKLOG_MAX_MS: 20000,
}))

function fakeSession(): STTSession {
  return {
    start: () => Promise.resolve(),
    stop: async () => {},
    onPartial: () => {}, onFinal: () => {}, onError: () => {}, onEnd: () => {},
  }
}

vi.mock('@/audio/resolver', () => ({
  resolveSttBackend: () => ({
    get kind() { return backendKind },
    isAvailable: () => true,
    create: (opts: { language: string }) => { created.push(opts); return fakeSession() },
  }),
}))

describe('the Automatic dictation language', () => {
  beforeEach(() => {
    created.length = 0
    Object.defineProperty(navigator, 'language', { value: 'el-GR', configurable: true })
  })

  it('is its own menu row, first, and ticks when stored', () => {
    expect(STT_MENU_OPTIONS[0]).toEqual({ value: AUTO_LANGUAGE, label: 'Automatic' })
    expect(currentSttLanguage('auto')).toBe(AUTO_LANGUAGE)
    expect(isAutoLanguage(' AUTO ')).toBe(true)
    // No pick keeps the browser default row, not Automatic.
    expect(currentSttLanguage(null)).toBe('el-GR')
    expect(currentSttLanguage('en-GB')).toBe('en-GB')
  })

  it('reaches the platform engine as "auto"', async () => {
    backendKind = 'platform'
    const { result } = renderHook(() => useSpeechSession({}))
    await act(async () => { await result.current.start() })
    expect(created[0].language).toBe('auto')
  })

  it('keeps the browser language on the device recognizer', async () => {
    backendKind = 'native'
    const { result } = renderHook(() => useSpeechSession({}))
    await act(async () => { await result.current.start() })
    expect(created[0].language).toBe('el')
  })

  it('a person who never picked keeps the browser language on either engine', async () => {
    prefs.stt_language = null
    backendKind = 'platform'
    const { result } = renderHook(() => useSpeechSession({}))
    await act(async () => { await result.current.start() })
    expect(created[0].language).toBe('el')
    prefs.stt_language = 'auto'
  })
})
