import { describe, expect, it } from 'vitest'
import { cleanForSpeech } from '@/audio/cleanText'

describe('cleanForSpeech', () => {
  it('drops emoji, dingbats, arrows and symbols', () => {
    expect(cleanForSpeech('Done 🎉 ✅ → next ⬆ step')).toBe('Done next step')
  })

  it('drops a joined emoji sequence whole: its joiner and variation selector too', () => {
    expect(cleanForSpeech('Team 👩‍💻 ready ❤️ now')).toBe('Team ready now')
  })

  it('drops a lone joiner and a lone variation selector', () => {
    expect(cleanForSpeech('a‍b️c')).toBe('abc')
  })

  it('leaves plain text, accents and other scripts alone', () => {
    expect(cleanForSpeech('Καλημέρα, café — 3 items.')).toBe('Καλημέρα, café — 3 items.')
  })
})
