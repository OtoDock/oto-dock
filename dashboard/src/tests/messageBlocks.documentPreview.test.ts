import { describe, it, expect } from 'vitest'
import { dbMessagesToDisplay, eventToBlock, liveBlockToMessageBlock } from '../lib/messageBlocks'

// A document push renders as a card per push (one per file per message);
// the history rebuild keeps every message's card, and the version number
// stamped on the push rides every path a block is built by.

const previewRow = (id: number, fileId: string, snapshotId: string, extra: object = {}) => ({
  id,
  role: 'event',
  event_type: 'document_preview',
  event_data: JSON.stringify({
    type: 'document_preview',
    wopi_url: `/cool?f=${fileId}`,
    filename: `${fileId}.xlsx`,
    file_id: fileId,
    download_url: `/dl/${fileId}`,
    snapshot_id: snapshotId,
    generation: id,
    ...extra,
  }),
  created_at: '2026-01-01T00:00:00Z',
})
const userRow = (id: number) => ({
  id, role: 'user', content: 'next turn please', created_at: '2026-01-01T00:00:00Z',
})

describe('document_preview history rebuild', () => {
  it('keeps a card per message, with its version identity', () => {
    const msgs = dbMessagesToDisplay(
      [previewRow(1, 'f1', 's1', { version: 1 }), userRow(2), previewRow(3, 'f1', 's2', { version: 2 })], [],
    )
    const previews = msgs.flatMap(m => m.blocks).filter(b => b.type === 'document_preview')
    expect(previews).toHaveLength(2)
    expect(previews[0]).toMatchObject({ snapshotId: 's1', generation: 1, version: 1, dbMessageId: 1 })
    expect(previews[1]).toMatchObject({ snapshotId: 's2', generation: 3, version: 2, dbMessageId: 3 })
  })

  it('dedupes intra-message pushes to the last per file', () => {
    // Terminal chats persist every push; one card per file per turn renders.
    const msgs = dbMessagesToDisplay(
      [previewRow(1, 'f1', 's1'), previewRow(2, 'f1', 's2'), previewRow(3, 'f2', 'x1')], [],
    )
    const previews = msgs.flatMap(m => m.blocks).filter(b => b.type === 'document_preview')
    expect(previews).toHaveLength(2)
    expect(previews[0]).toMatchObject({ fileId: 'f1', snapshotId: 's2' })
    expect(previews[1]).toMatchObject({ fileId: 'f2', snapshotId: 'x1' })
  })

  it('skips rows closed before the document pane', () => {
    const msgs = dbMessagesToDisplay(
      [previewRow(1, 'f1', 's1', { dismissed: true }), userRow(2), previewRow(3, 'f1', 's2')], [],
    )
    const previews = msgs.flatMap(m => m.blocks).filter(b => b.type === 'document_preview')
    expect(previews).toHaveLength(1)
    expect(previews[0]).toMatchObject({ snapshotId: 's2' })
  })
})

describe('the version number on every block path', () => {
  const frame = {
    type: 'document_preview', wopi_url: '/c', filename: 'a.docx',
    file_id: 'f1', download_url: '/d', snapshot_id: 'sX', generation: 7, version: 3,
  }

  it('eventToBlock carries the version and the snapshot identity', () => {
    expect(eventToBlock(frame, 42)).toMatchObject({ snapshotId: 'sX', generation: 7, version: 3, dbMessageId: 42 })
    const old = eventToBlock({ type: 'document_preview', wopi_url: '/c', filename: 'a.docx', file_id: 'f1', download_url: '/d' }, 41)
    expect(old).toMatchObject({ type: 'document_preview' })
    expect((old as { version?: number; snapshotId?: string }).version).toBeUndefined()
    expect((old as { snapshotId?: string }).snapshotId).toBeUndefined()
  })

  it('a reconnect live state maps it too, without the pushed token', () => {
    const block = liveBlockToMessageBlock({ ...frame, access_token: 't' })
    expect(block).toMatchObject({ type: 'document_preview', version: 3, snapshotId: 'sX' })
    expect(JSON.stringify(block)).not.toContain('"t"')
  })
})
