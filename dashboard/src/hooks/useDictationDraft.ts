// The composer's dictation draft: live interims REPLACE (a preview over the
// committed text), finals ACCUMULATE, and a language switch from the platform
// engine REWRITES exactly the words the session placed. Bound to the chat the
// dictation started in: after a switch the old attempt's interims, finals and
// replaces are dropped (this also keeps the duplex caption bridge's
// deactivation, an empty interim fired once its session ends on the switch,
// from wiping the new chat's restored draft).

import { useRef, type RefObject } from 'react'

export const joinText = (a: string, b: string) =>
  a ? `${a}${a.endsWith(' ') || a.endsWith('\n') ? '' : ' '}${b}` : b

const WORD_CHAR = /[\p{L}\p{N}]/u

// The last place `placed` sits in `text` as whole words (no letter or digit
// right before or after it), or -1.
export function findWords(text: string, placed: string): number {
  for (let at = text.lastIndexOf(placed); at >= 0; at = at ? text.lastIndexOf(placed, at - 1) : -1) {
    const before = at > 0 ? text[at - 1] : ''
    const after = text[at + placed.length] ?? ''
    if (!WORD_CHAR.test(before) && !WORD_CHAR.test(after)) return at
  }
  return -1
}

export function useDictationDraft({ valueRef, draftKeyRef, setText, onStart, afterChange }: {
  valueRef: RefObject<string | undefined>   // the composer's current text
  draftKeyRef: RefObject<string | undefined>  // the chat the composer belongs to
  setText: (text: string) => void
  onStart: () => void                 // dictation engaged (the composer grows into view)
  afterChange: () => void             // a programmatic set bypasses the input handler's resize
}) {
  // The committed text the live interim is appended onto.
  const baseRef = useRef('')
  // The words this dictation placed (its finals joined, or the last replace's
  // text and the finals after it), and where they start in the committed
  // text: a language switch replaces exactly these, and skips when they
  // were edited.
  const placedRef = useRef('')
  const placedAtRef = useRef(-1)
  const keyRef = useRef('')   // the chat the dictation started in (set when the mic engages)
  const dictatingRef = useRef(false)   // mic actively dictating → manual edits re-baseline

  const here = () => keyRef.current === (draftKeyRef.current ?? '')

  // Dictation started → snapshot whatever is already in the input as the base
  // (live partials append onto it).
  const onMicActive = (active: boolean) => {
    dictatingRef.current = active
    if (active) {
      onStart()
      keyRef.current = draftKeyRef.current ?? ''
      baseRef.current = valueRef.current ?? ''
      placedRef.current = ''
      placedAtRef.current = -1
    }
  }
  // Live partial → show base+interim without committing (the next partial/final replaces it).
  const showInterim = (t: string) => {
    if (!here()) return
    setText(joinText(baseRef.current, t))
    afterChange()
  }
  // Finalized phrase → commit it onto the base so the next utterance builds after it.
  const appendTranscript = (t: string) => {
    if (!here()) return
    baseRef.current = joinText(baseRef.current, t)
    if (!placedRef.current) placedAtRef.current = baseRef.current.length - t.length
    placedRef.current = joinText(placedRef.current, t)
    setText(baseRef.current)
    afterChange()
  }
  // The platform engine switched the dictation's language: the words this
  // session placed are `t` now (the other language's reading of the same
  // speech). The placed words are spliced where the session put them (the
  // stored offset, checked), else where they last sit as whole words, so
  // what the person typed around them stays; when the placed words
  // themselves were edited (not found) the new reading is appended instead,
  // so the other language's words are never lost. The head loses only the
  // one space `joinText` added (a line break before the dictation stays).
  const replaceTranscript = (t: string) => {
    if (!here()) return
    const base = baseRef.current
    const placed = placedRef.current
    let head = base
    let tail = ''
    if (placed) {
      let at = placedAtRef.current
      if (at < 0 || base.slice(at, at + placed.length) !== placed) at = findWords(base, placed)
      if (at < 0) {
        baseRef.current = t ? joinText(base, t) : base
        placedAtRef.current = t ? baseRef.current.length - t.length : -1
        placedRef.current = t
        setText(baseRef.current)
        afterChange()
        return
      }
      head = base.slice(0, at).replace(/ $/, '')
      tail = base.slice(at + placed.length)
    }
    const joined = t ? joinText(head, t) : head
    baseRef.current = joined ? joined + tail : tail.replace(/^ /, '')
    placedAtRef.current = t ? joined.length - t.length : -1
    placedRef.current = t
    setText(baseRef.current)
    afterChange()
  }
  // A manual edit mid-dictation: the next partial or final builds on the
  // edited text (otherwise a deleted phrase reappears on the next utterance),
  // and an edit before the placed words moves their offset with them.
  const rebaseline = (value: string) => {
    if (!dictatingRef.current) return
    const old = baseRef.current
    const at = placedAtRef.current
    if (at >= 0) {
      let same = 0
      const n = Math.min(old.length, value.length)
      while (same < n && old[same] === value[same]) same++
      if (same < at) placedAtRef.current = at + value.length - old.length
    }
    baseRef.current = value
  }
  // A send or a chat switch: the accumulator starts over (a late stop-flush
  // final must not re-fill the cleared input).
  const reset = (endDictation = false) => {
    baseRef.current = ''
    placedRef.current = ''
    placedAtRef.current = -1
    if (endDictation) dictatingRef.current = false
  }

  return { onMicActive, showInterim, appendTranscript, replaceTranscript, rebaseline, reset }
}
