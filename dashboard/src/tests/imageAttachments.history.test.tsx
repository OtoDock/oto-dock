/**
 * Past chat photos render after a reload.
 *
 * The backend now persists each attachment's saved upload path in the user
 * row's event_data; history rows map it onto the image_attachments block and
 * the renderer serves the photo through the agent files API. Rows persisted
 * before paths existed (or views without an agent slug) keep the count badge.
 */
import { describe, it, expect } from 'vitest'
import { render, screen } from '@testing-library/react'
import { dbMessagesToDisplay } from '@/lib/messageBlocks'
import BlockRenderer from '@/components/chat/ChatBlockRenderer'

const PHOTO_PATH = 'users/admin/workspace/uploads/photos/img_ab12cd34.png'

function userRow(eventData: object) {
  return {
    id: 1,
    role: 'user',
    content: 'look at this',
    event_type: '',
    event_data: JSON.stringify(eventData),
    created_at: '2026-07-07T00:00:00+00:00',
  }
}

describe('image_attachments history rendering', () => {
  it('maps saved paths from event_data onto the block', () => {
    const msgs = dbMessagesToDisplay(
      [userRow({ images: [{ name: 'pic.png', path: PHOTO_PATH }] })],
      undefined,
    )
    const block = msgs[0].blocks.find((b) => b.type === 'image_attachments')
    expect(block).toEqual({
      type: 'image_attachments',
      images: ['pic.png'],
      paths: [PHOTO_PATH],
    })
  })

  it('maps legacy rows without paths to null entries', () => {
    const msgs = dbMessagesToDisplay(
      [userRow({ images: [{ name: 'pic.png' }] })],
      undefined,
    )
    const block = msgs[0].blocks.find((b) => b.type === 'image_attachments')
    expect(block).toEqual({
      type: 'image_attachments',
      images: ['pic.png'],
      paths: [null],
    })
  })

  it('renders past photos through the agent files API', () => {
    render(
      <BlockRenderer
        block={{ type: 'image_attachments', images: ['pic.png'],
                 paths: [PHOTO_PATH] }}
        blockId="m1-b0"
        blockOrder={0}
        isUserMessage
        agentName="my-agent"
        onPermissionRespond={() => {}}
      />,
    )
    const img = screen.getByAltText('Attached image 1') as HTMLImageElement
    expect(img.getAttribute('src')).toBe(
      `/v1/agents/my-agent/files/${PHOTO_PATH}`,
    )
  })

  it('keeps the count badge for legacy rows and agent-less views', () => {
    const { rerender } = render(
      <BlockRenderer
        block={{ type: 'image_attachments', images: ['a.png', 'b.png'],
                 paths: [null, null] }}
        blockId="m1-b0"
        blockOrder={0}
        isUserMessage
        agentName="my-agent"
        onPermissionRespond={() => {}}
      />,
    )
    expect(screen.getByText('2 images attached')).toBeTruthy()
    expect(screen.queryByAltText('Attached image 1')).toBeNull()

    rerender(
      <BlockRenderer
        block={{ type: 'image_attachments', images: ['a.png'],
                 paths: [PHOTO_PATH] }}
        blockId="m1-b0"
        blockOrder={0}
        isUserMessage
        onPermissionRespond={() => {}}
      />,
    )
    expect(screen.getByText('1 image attached')).toBeTruthy()
  })
})
