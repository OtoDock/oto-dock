import { describe, expect, it } from 'vitest'

import {
  dbMessagesToDisplay,
  eventToBlock,
  previewChainModes,
} from '../lib/messageBlocks'
import type { DisplayMessage, MessageBlock } from '../components/chat/types'

function preview(fileId: string, snapshotId?: string): MessageBlock {
  return {
    type: 'document_preview',
    wopiUrl: `/collabora/cool.html?file=${fileId}`,
    filename: `${fileId}.xlsx`,
    fileId,
    downloadUrl: `/dl/${fileId}`,
    ...(snapshotId ? { snapshotId } : {}),
  }
}

function msg(id: string, blocks: MessageBlock[]): DisplayMessage {
  return { id, role: 'assistant', blocks, createdAt: '2026-01-01T00:00:00Z' }
}

describe('previewChainModes', () => {
  it('single instance renders live', () => {
    const modes = previewChainModes([msg('m1', [preview('f1')])])
    expect(modes.get('0:0')).toBe('live')
  })

  it('two instances: older frozen, newest live', () => {
    const modes = previewChainModes([
      msg('m1', [{ type: 'text', content: 'a' } as MessageBlock, preview('f1', 's1')]),
      msg('m2', [preview('f1', 's2')]),
    ])
    expect(modes.get('0:1')).toBe('frozen')
    expect(modes.get('1:0')).toBe('live')
  })

  it('three instances: oldest chips, middle frozen, newest live', () => {
    const modes = previewChainModes([
      msg('m1', [preview('f1', 's1')]),
      msg('m2', [preview('f1', 's2')]),
      msg('m3', [preview('f1', 's3')]),
    ])
    expect(modes.get('0:0')).toBe('chip')
    expect(modes.get('1:0')).toBe('frozen')
    expect(modes.get('2:0')).toBe('live')
  })

  it('frozen block keeps ITS OWN snapshot, never the incoming push\'s', () => {
    // The worst failure class: a "previous version" block rendering NEW
    // content. The frozen instance must be the FIRST push's block — its own
    // snapshotId — not anything from the second push's event.
    const msgs = [msg('m1', [preview('f1', 's1')]), msg('m2', [preview('f1', 's2')])]
    const modes = previewChainModes(msgs)
    const frozenKey = [...modes.entries()].find(([, m]) => m === 'frozen')![0]
    const [mi, bi] = frozenKey.split(':').map(Number)
    const frozenBlock = msgs[mi].blocks[bi]
    expect(frozenBlock).toMatchObject({ type: 'document_preview', snapshotId: 's1' })
    expect((frozenBlock as { snapshotId?: string }).snapshotId).not.toBe('s2')
  })

  it('an interleaved text turn does not touch the live block (deferred-drain regression)', () => {
    // Round-1 bug: the positional collapse ("all but last message") ran after
    // a text-only turn and collapsed the file's only preview — zero live
    // blocks, dead chip anchor. The render-time chain keys on occurrence
    // order per file, so the last instance stays live wherever it sits.
    const modes = previewChainModes([
      msg('m1', [preview('f1')]),
      msg('m2', [{ type: 'text', content: 'a later turn without previews' } as MessageBlock]),
    ])
    expect(modes.get('0:0')).toBe('live')
  })

  it('loadOlder prepends only frozen/chip entries — the latest stays live', () => {
    const newest = [msg('m3', [preview('f1', 's3')])]
    const withOlder = [
      msg('m1', [preview('f1', 's1')]),
      msg('m2', [preview('f1', 's2')]),
      ...newest,
    ]
    expect(previewChainModes(newest).get('0:0')).toBe('live')
    const modes = previewChainModes(withOlder)
    expect(modes.get('2:0')).toBe('live')
    expect(modes.get('1:0')).toBe('frozen')
    expect(modes.get('0:0')).toBe('chip')
  })

  it('files chain independently', () => {
    const modes = previewChainModes([
      msg('m1', [preview('f1'), preview('f2')]),
      msg('m2', [preview('f1')]),
    ])
    expect(modes.get('0:0')).toBe('frozen')
    expect(modes.get('0:1')).toBe('live') // f2's only instance
    expect(modes.get('1:0')).toBe('live')
  })
})

describe('document_preview history rebuild', () => {
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

  it('keeps cross-message instances (chain input), maps snapshot fields', () => {
    const msgs = dbMessagesToDisplay(
      [previewRow(1, 'f1', 's1'), userRow(2), previewRow(3, 'f1', 's2')], [],
    )
    const previews = msgs.flatMap(m => m.blocks).filter(b => b.type === 'document_preview')
    expect(previews).toHaveLength(2)
    expect(previews[0]).toMatchObject({ snapshotId: 's1', generation: 1 })
    expect(previews[1]).toMatchObject({ snapshotId: 's2', generation: 3 })
  })

  it('dedupes intra-message pushes to the last per file', () => {
    // Interactive chats persist every intra-turn push; only the turn's final
    // state renders (the pump path already dedupes server-side).
    const msgs = dbMessagesToDisplay(
      [previewRow(1, 'f1', 's1'), previewRow(2, 'f1', 's2'), previewRow(3, 'f2', 'x1')], [],
    )
    const previews = msgs.flatMap(m => m.blocks).filter(b => b.type === 'document_preview')
    expect(previews).toHaveLength(2)
    expect(previews[0]).toMatchObject({ fileId: 'f1', snapshotId: 's2' })
    expect(previews[1]).toMatchObject({ fileId: 'f2', snapshotId: 'x1' })
  })

  it('skips dismissed instances', () => {
    const msgs = dbMessagesToDisplay(
      [previewRow(1, 'f1', 's1', { dismissed: true }), userRow(2), previewRow(3, 'f1', 's2')], [],
    )
    const previews = msgs.flatMap(m => m.blocks).filter(b => b.type === 'document_preview')
    expect(previews).toHaveLength(1)
    expect(previews[0]).toMatchObject({ snapshotId: 's2' })
  })

  it('eventToBlock carries snapshot identity; absent fields stay undefined', () => {
    const withSnap = eventToBlock({
      type: 'document_preview', wopi_url: '/c', filename: 'a.docx',
      file_id: 'f1', download_url: '/d', snapshot_id: 'sX', generation: 7,
    }, 42)
    expect(withSnap).toMatchObject({ snapshotId: 'sX', generation: 7, dbMessageId: 42 })
    const preSnapshot = eventToBlock({
      type: 'document_preview', wopi_url: '/c', filename: 'a.docx',
      file_id: 'f1', download_url: '/d',
    }, 41)
    expect(preSnapshot).toMatchObject({ type: 'document_preview' })
    expect((preSnapshot as { snapshotId?: string }).snapshotId).toBeUndefined()
  })
})
