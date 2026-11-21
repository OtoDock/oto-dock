/**
 * The wrap-up after a call is a `wrapup` event row at the end of a phone
 * conversation. History rebuilds it into its block (the row's data carries
 * no `type`, its column names it) and the card says how it ended, the
 * outcome, the follow-ups and what it wrote.
 */
import { describe, it, expect } from 'vitest'
import { render, screen, within } from '@testing-library/react'

import BlockRenderer from '@/components/chat/ChatBlockRenderer'
import type { MessageBlock } from '@/components/chat/types'
import { EVENT_ROW, FRAMES, WIRE } from '@/api/wireEvents'
import { dbMessagesToDisplay, eventToBlock } from '@/lib/messageBlocks'
import { WRAPUP_STATE } from '@/lib/status/wrapup'

let nextId = 1
function row(role: string, content: string, eventType = '', eventData: object | null = null) {
  return {
    id: nextId++, role, content, event_type: eventType,
    event_data: eventData ? JSON.stringify(eventData) : '', created_at: '2026-11-07T10:00:00+00:00',
  }
}

const BASE = { outcome: '', follow_ups: [], wrote: [], run_id: 'run-1' }

/** The block the history makes of a phone conversation's wrap-up row. */
function blockOf(data: object): Extract<MessageBlock, { type: 'wrapup' }> {
  const msgs = dbMessagesToDisplay(
    [row('user', 'Hello'), row('assistant', 'Hi'), row('event', '', 'wrapup', data)], [],
  )
  const b = msgs.flatMap((m) => m.blocks).find((x) => x.type === 'wrapup')
  if (!b || b.type !== 'wrapup') throw new Error('no wrapup block')
  return b
}

function renderCard(data: object) {
  render(
    <BlockRenderer block={blockOf(data)} blockId="m1-b0" blockOrder={0} isUserMessage={false} onPermissionRespond={() => {}} />,
  )
  return screen.getByTestId('wrapup-card')
}

describe('the wrap-up event row', () => {
  it('is named beside the frames, not as a frame', () => {
    expect(EVENT_ROW.WRAPUP).toBe('wrapup')
    expect(Object.values(WIRE)).not.toContain(EVENT_ROW.WRAPUP)
    expect(Object.keys(FRAMES)).not.toContain(EVENT_ROW.WRAPUP)
  })

  it('rebuilds into the block with the card fields, a row with no type too', () => {
    expect(blockOf({
      ...BASE, state: 'done', outcome: 'Booked.', follow_ups: ['Call back', ''],
      wrote: [{ kind: 'memory', name: 'prefs.md' }, { kind: 'file', name: 'report.md' }],
    })).toEqual({
      type: 'wrapup', state: 'done', outcome: 'Booked.', followUps: ['Call back'],
      wrote: [{ kind: 'memory', name: 'prefs.md' }, { kind: 'file', name: 'report.md' }],
    })
  })

  it('tolerates a bare row', () => {
    expect(eventToBlock({ type: EVENT_ROW.WRAPUP })).toEqual({
      type: 'wrapup', state: '', outcome: '', followUps: [], wrote: [],
    })
  })

  it('a dismissed row draws nothing', () => {
    const rows = [row('assistant', 'Hi'), row('event', '', 'wrapup', { ...BASE, state: 'done', dismissed: true })]
    expect(dbMessagesToDisplay(rows, []).flatMap((m) => m.blocks).some((b) => b.type === 'wrapup')).toBe(false)
  })
})

describe('the wrap-up card', () => {
  it('skipped says nobody spoke', () => {
    const card = renderCard({ ...BASE, state: WRAPUP_STATE.SKIPPED, outcome: 'No answer.' })
    expect(within(card).getByText('Nobody spoke on this call.')).toBeInTheDocument()
    expect(within(card).getByText('No answer.')).toBeInTheDocument()
  })

  it('failed says so and shows the outcome when there is one', () => {
    const card = renderCard({ ...BASE, state: WRAPUP_STATE.FAILED, outcome: 'The caller asked for a table.' })
    expect(within(card).getByText('The wrap-up did not finish.')).toBeInTheDocument()
    expect(within(card).getByText('Outcome')).toBeInTheDocument()
    expect(within(card).getByText('The caller asked for a table.')).toBeInTheDocument()
  })

  it('failed with no outcome says it once', () => {
    const card = renderCard({ ...BASE, state: WRAPUP_STATE.FAILED })
    expect(within(card).getByText('The wrap-up did not finish.')).toBeInTheDocument()
    expect(within(card).queryByText('Outcome')).toBeNull()
  })

  it('failed with the same words for an outcome says it once', () => {
    const card = renderCard({ ...BASE, state: WRAPUP_STATE.FAILED, outcome: 'The wrap-up did not finish.' })
    expect(within(card).getAllByText('The wrap-up did not finish.')).toHaveLength(1)
    expect(within(card).queryByText('Outcome')).toBeNull()
  })

  it('done adds no line and shows the outcome, the follow-ups and what it wrote', () => {
    const card = renderCard({
      ...BASE, state: WRAPUP_STATE.DONE, outcome: 'Booked a table for four on Friday.',
      follow_ups: ['Confirm on Thursday', 'Ask about allergies'],
      wrote: [{ kind: 'memory', name: 'preferences.md' }, { kind: 'file', name: 'report.md' }],
    })
    for (const line of ['Nobody spoke on this call.', 'The wrap-up did not finish.']) {
      expect(within(card).queryByText(line)).toBeNull()
    }
    expect(within(card).getByText('Outcome')).toBeInTheDocument()
    expect(within(card).getByText('Booked a table for four on Friday.')).toBeInTheDocument()
    expect(within(card).getByText('Follow-ups')).toBeInTheDocument()
    expect(within(card).getAllByRole('list')).toHaveLength(2)
    expect(within(card).getByText('Confirm on Thursday')).toBeInTheDocument()
    expect(within(card).getByText('Ask about allergies')).toBeInTheDocument()
    expect(within(card).getByText('Written')).toBeInTheDocument()
    const written = within(card).getAllByRole('listitem').map((li) => li.textContent)
    expect(written).toContain('Memory: preferences.md')
    expect(written).toContain('File: report.md')
  })

  it('done with nothing recorded shows the title alone', () => {
    const card = renderCard({ ...BASE, state: WRAPUP_STATE.DONE })
    expect(card.textContent).toBe('After the call')
  })
})
