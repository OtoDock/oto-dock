// Chat-audio backend interfaces — a uniform surface over native (Web Speech /
// Capacitor) and platform (server endpoint / WS) engines so the SoundIcon /
// MicIcon and the resolver don't care which is in use.

export type AudioMode = 'native' | 'platform' | 'auto'
export type Availability = 'unavailable' | 'native_only' | 'platform' | 'either'

export interface ChatAudioCapability {
  tts: Availability
  stt: Availability
  tts_provider_id: number | null
  stt_provider_id: number | null
  reason: string
  icons_enabled: boolean
  /** Full-duplex conversation mode (phone engine on this chat). Absent on
   *  older proxies — absent means unavailable (fail-closed by design). */
  duplex?: { available: boolean; reason: string }
}

export interface TTSPlayOptions {
  language: string
  voiceURI?: string          // native voice (device-specific)
  voiceId?: string           // platform voice override
  providerId?: number | null // platform provider
}

export interface TtsStream {
  /** Feed one complete sentence to speak — queued/streamed in order. */
  push(text: string): void
  /** No more text — let queued audio finish playing, then resolve `done`. */
  finish(): void
  /** Barge-in / abort — stop immediately and resolve `done`. */
  cancel(): void
  /** Resolves when playback has finished (or cancel/finish has settled it). */
  done: Promise<void>
}

export interface TTSBackend {
  kind: 'native' | 'platform'
  isAvailable(): boolean
  play(text: string, opts: TTSPlayOptions): Promise<void>
  stop(): void
  /** Incremental sink for voice mode — speak sentences as a reply generates.
   *  Native backends queue utterances; the platform backend streams over WS. */
  createStream(opts: TTSPlayOptions): TtsStream
}

export interface STTSession {
  start(): Promise<void>
  stop(): Promise<void>
  onPartial(handler: (text: string) => void): void
  onFinal(handler: (text: string) => void): void
  onError(handler: (err: Error) => void): void
  // Fires when the recognizer stops on its own (silence / single-utterance end)
  // so the UI can leave the "recording" state without a manual stop.
  onEnd(handler: () => void): void
}

export interface STTCreateOptions {
  language: string
  providerId?: number | null
}

export interface STTBackend {
  kind: 'native' | 'platform'
  isAvailable(): boolean
  create(opts: STTCreateOptions): STTSession
}

/** Synchronous Capacitor-native detection (the global is injected on device). */
export function isNativePlatform(): boolean {
  const cap = (window as unknown as { Capacitor?: { isNativePlatform?: () => boolean } }).Capacitor
  return !!cap?.isNativePlatform?.()
}

/** Bounded wait for a plugin availability probe: a wedged speech engine can
 * hold the plugin call open indefinitely (device report 2026-07-12), which
 * must read as "not available now" — never block the capability resolve. */
export function probeWithTimeout(probe: Promise<boolean>, ms = 4000): Promise<boolean> {
  return Promise.race([probe, new Promise<boolean>(r => setTimeout(() => r(false), ms))])
}
