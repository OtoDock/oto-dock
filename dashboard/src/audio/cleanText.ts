// Strip markdown syntax + emoji/symbols so TTS speaks prose, not "## hash hash"
// / bullet stars / table pipes / icons. Shared by the SoundIcon (one-shot replay)
// (per-message read-aloud). Non-text blocks (tool/image/etc.)
// are already excluded upstream by extractPlainText.

export function cleanForSpeech(text: string): string {
  return text
    .replace(/```[\s\S]*?```/g, ' ')              // fenced code
    .replace(/`([^`]+)`/g, '$1')                  // inline code
    .replace(/!\[[^\]]*\]\([^)]*\)/g, ' ')        // images
    .replace(/\[([^\]]+)\]\([^)]*\)/g, '$1')      // links → label
    .replace(/^\s{0,3}#{1,6}\s+/gm, '')           // headings
    .replace(/^\s{0,3}>+\s?/gm, '')               // blockquotes
    .replace(/^\s*[-*+]\s+/gm, '')                // bullet markers
    .replace(/^\s*\d+\.\s+/gm, '')                // numbered markers
    .replace(/^\s*\|.*\|\s*$/gm, ' ')             // table rows
    .replace(/^[\s|:-]{3,}$/gm, ' ')              // table separators / hr
    .replace(/(\*\*\*|\*\*|\*|___|__|_|~~)/g, '') // bold / italic / strike
    // emoji, dingbats, arrows, symbols, variation selectors, ZWJ
    .replace(/[\u{1F000}-\u{1FFFF}\u{2600}-\u{27BF}\u{2190}-\u{21FF}\u{2B00}-\u{2BFF}\u{FE0F}\u{200D}]/gu, '')
    .replace(/[ \t]{2,}/g, ' ')
    .replace(/\n{2,}/g, '\n')
    .trim()
}
