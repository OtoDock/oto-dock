import { describe, it, expect } from 'vitest'

import { pasteAttachesFiles } from '@/lib/composerKeys'

// A paste with files on the clipboard: the files attach when the text beside
// them only names them; prose or cells beside a picture (Word, Excel,
// LibreOffice) are inserted as text instead.

const shot = [{ name: 'Screenshot 2026-10-08 at 10.15.04.png' }]
const doc = [{ name: 'Quarterly report.docx' }]

describe('pasteAttachesFiles', () => {
  it('attaches when there is no text', () => {
    expect(pasteAttachesFiles('', shot)).toBe(true)
    expect(pasteAttachesFiles('  \n\t ', shot)).toBe(true)
    expect(pasteAttachesFiles('\uFFFC', shot)).toBe(true)
    expect(pasteAttachesFiles(' \uFFFC\n', shot)).toBe(true)
  })

  it('attaches when the text is the file name, with or without its extension', () => {
    expect(pasteAttachesFiles('Quarterly report.docx', doc)).toBe(true)
    expect(pasteAttachesFiles('Quarterly report', doc)).toBe(true)
    expect(pasteAttachesFiles('quarterly REPORT.DOCX', doc)).toBe(true)
    // Composed and decomposed forms of the same name match.
    expect(pasteAttachesFiles('Cafe\u0301.png', [{ name: 'Café.png' }])).toBe(true)
  })

  it('attaches when every line is a path, a file URI or the one address', () => {
    expect(pasteAttachesFiles('/home/me/a.png\n/home/me/b.png', shot)).toBe(true)
    expect(pasteAttachesFiles('C:\\Users\\me\\a.png', shot)).toBe(true)
    expect(pasteAttachesFiles('\\\\server\\share\\a.png', shot)).toBe(true)
    expect(pasteAttachesFiles('~/Desktop/a.png', shot)).toBe(true)
    expect(pasteAttachesFiles('file:///home/me/My%20photo.png', shot)).toBe(true)
    expect(pasteAttachesFiles('https://example.com/cat.png', shot)).toBe(true)
  })

  it('keeps the text when it is prose or cells beside a picture', () => {
    expect(pasteAttachesFiles('Revenue grew 12% this quarter.', shot)).toBe(false)
    expect(pasteAttachesFiles('Q3\t1200\nQ4\t1500', [{ name: 'image.png' }])).toBe(false)
    expect(pasteAttachesFiles('Quarterly report.docx\nplus some notes', doc)).toBe(false)
    expect(pasteAttachesFiles('https://a.example/x https://b.example/y', shot)).toBe(false)
    expect(pasteAttachesFiles('https://a.example/x\nhttps://b.example/y', shot)).toBe(false)
    // A file URI that does not decode is not a file URI.
    expect(pasteAttachesFiles('file:///bad%E0%A4%A', shot)).toBe(false)
  })

  it('keeps an office cell that holds an address or a path', () => {
    const cell = '<table><tr><td>https://example.com/report</td></tr></table>'
    expect(pasteAttachesFiles('https://example.com/report', shot, cell)).toBe(false)
    expect(pasteAttachesFiles('/srv/reports/q3.xlsx', shot, '{\\rtf1 /srv/reports}')).toBe(false)
    // A browser's image copy carries the picture in its html: it attaches.
    expect(pasteAttachesFiles('https://example.com/cat.png', shot,
                              '<img src="https://example.com/cat.png">')).toBe(true)
    // With no text at all the files win whatever the rich text says.
    expect(pasteAttachesFiles('', shot, '<table></table>')).toBe(true)
  })
})
