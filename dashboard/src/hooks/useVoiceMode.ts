// Voice mode orchestration — when enabled, speak the assistant's reply aloud as
// it streams (from the first sentence), through whichever TTS backend the user's
// capability + prefs resolve to: native browser (speechSynthesis), native Android
// (Capacitor), or the platform streaming WS. All three are uniform behind
// TtsStream, so this hook never knows which engine is talking.
//
// One spoken stream per reply, keyed by the last assistant message's id.
// extractFlushableSentences feeds only complete sentences (trailing-lookahead
// margin + code-fence guard); on stream completion the remainder is flushed and
// finish()ed. cancel() is barge-in (and suppresses restart for that reply).

import { useCallback, useEffect, useRef, useState } from 'react'
import type { DisplayMessage } from '../components/chat/types'
import { useChatAudioCapability } from './useChatAudioCapability'
import { useMyAudioPrefs } from '../api/userAudio'
import { useAudioPrefsStore } from '../store/audioPrefsStore'
import { resolveTtsBackend } from '../audio/resolver'
import { detectTtsLanguage } from '../audio/lang'
import { extractFlushableSentences } from '../audio/voiceFeed'
import type { TtsStream } from '../audio/types'

export interface VoiceModeController {
  speaking: boolean
  /** Barge-in: stop the current spoken reply and don't restart it. */
  cancel: () => void
}

function lastAssistant(messages: DisplayMessage[]): DisplayMessage | null {
  for (let i = messages.length - 1; i >= 0; i--) {
    if (messages[i].role === 'assistant') return messages[i]
  }
  return null
}

function replyText(msg: DisplayMessage): string {
  return msg.blocks
    .filter((b): b is { type: 'text'; content: string } => b.type === 'text')
    .map(b => b.content)
    .join('\n\n')
}

export function useVoiceMode(messages: DisplayMessage[], streaming: boolean): VoiceModeController {
  const enabled = useAudioPrefsStore(s => s.voiceModeEnabled)
  const nativeVoiceURI = useAudioPrefsStore(s => s.nativeVoiceURI)
  const { data: cap } = useChatAudioCapability()
  const { data: prefs } = useMyAudioPrefs()
  const [speaking, setSpeaking] = useState(false)

  const streamRef = useRef<TtsStream | null>(null)
  const streamFinishedRef = useRef(false)   // current stream was finish()ed at a boundary
  const spokenIdRef = useRef<string | null>(null)     // reply id we're speaking / have seen
  const cancelledIdRef = useRef<string | null>(null)  // reply id the user barged-in on
  const cursorRef = useRef(0)

  const canSpeak = enabled && !!cap && cap.tts !== 'unavailable'

  const stopStream = useCallback(() => {
    streamRef.current?.cancel()
    streamRef.current = null
    streamFinishedRef.current = false
    setSpeaking(false)
  }, [])

  const cancel = useCallback(() => {
    cancelledIdRef.current = spokenIdRef.current
    stopStream()
  }, [stopStream])

  // Stop speaking on unmount (navigating away / chat close).
  useEffect(() => () => stopStream(), [stopStream])

  useEffect(() => {
    if (!canSpeak) { if (streamRef.current) stopStream(); return }
    const msg = lastAssistant(messages)
    if (!msg) return

    // A new reply (new message id) supersedes any prior one.
    if (spokenIdRef.current !== msg.id) {
      if (streamRef.current) stopStream()
      spokenIdRef.current = msg.id
      cursorRef.current = 0
      // Don't auto-read a historical / already-finished message (e.g. on toggle-on
      // or chat open) — only speak a reply that is actively streaming.
      if (!streaming) return
    }
    if (msg.id === cancelledIdRef.current) return  // user barged-in on this reply

    const text = replyText(msg)
    // Flush held sentences at a hard boundary: the turn ended, OR the agent moved
    // on to a non-text block (a tool call) — the preceding sentence is complete, so
    // speak it NOW rather than waiting for the next text (which may be many tool
    // calls away). The TTS stream is only finish()ed at the REAL turn end, so it
    // stays open across tools and continues for the post-tool text.
    const streamEnded = !streaming
    const lastBlock = msg.blocks[msg.blocks.length - 1]
    const atBoundary = streamEnded || (!!lastBlock && lastBlock.type !== 'text')
    const { sentences, cursor } = extractFlushableSentences(text, cursorRef.current, atBoundary)
    cursorRef.current = cursor
    if (!sentences.length && !(streamEnded && streamRef.current)) return

    // Open the stream lazily on the FIRST real content so the reply's language is
    // detectable (baked into the platform init / native voice pick).
    if (sentences.length && (!streamRef.current || streamFinishedRef.current) && cap) {
      const backend = resolveTtsBackend(cap, prefs?.tts_mode ?? 'auto')
      if (!backend) return
      const lang = detectTtsLanguage(sentences.join(' '))
      const stream = backend.createStream({
        language: lang,
        voiceURI: nativeVoiceURI[lang],
        providerId: cap.tts_provider_id,
      })
      streamRef.current = stream
      streamFinishedRef.current = false
      setSpeaking(true)
      stream.done.then(() => {
        if (streamRef.current === stream) { streamRef.current = null; setSpeaking(false) }
      })
    }
    if (!streamFinishedRef.current) for (const s of sentences) streamRef.current?.push(s)
    // Finish (close) the TTS at EVERY boundary — a tool call OR the turn end — so a
    // short-lived stream per text-run can't outlive the provider's idle context
    // window across long tool gaps (later sentences would otherwise be silently
    // dropped). The next text-run opens a fresh stream.
    if (atBoundary && streamRef.current && !streamFinishedRef.current) {
      streamRef.current.finish()
      streamFinishedRef.current = true
    }
  }, [messages, streaming, canSpeak, cap, prefs, nativeVoiceURI, stopStream])

  return { speaking, cancel }
}
