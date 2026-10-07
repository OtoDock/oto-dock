/**
 * The undelivered-input card (`system` row, subtype `undelivered_input`):
 * a message from the chat's queue (any reason) says it was not sent and
 * why; a starting terminal's abandoned text keeps the cautious wording.
 * Both show the text whole so the person can copy it back.
 */
import { describe, it, expect } from 'vitest'
import { render, screen } from '@testing-library/react'
import SystemEvent from '@/components/chat/SystemEvent'
import { SYSTEM_SUBTYPE, UNDELIVERED_REASON } from '@/api/wireEvents'
import { eventToBlock, liveBlockToMessageBlock } from '@/lib/messageBlocks'

describe('the undelivered-input card', () => {
  it('says a queued message was not sent', () => {
    render(<SystemEvent subtype={SYSTEM_SUBTYPE.UNDELIVERED_INPUT} reason={UNDELIVERED_REASON.QUEUED}
                        message={'first\n\nsecond'} />)
    expect(screen.getByText('This message was not sent')).toBeTruthy()
    expect(screen.getByText(/could not be sent as the next turn/)).toBeTruthy()
    expect(screen.getByText(/first\s+second/)).toBeTruthy()
  })

  it('says why a message behind a failed or stopped turn was not sent', () => {
    const { unmount } = render(<SystemEvent subtype={SYSTEM_SUBTYPE.UNDELIVERED_INPUT}
                                            reason={UNDELIVERED_REASON.TURN_FAILED} message="one" />)
    expect(screen.getByText('This message was not sent')).toBeTruthy()
    expect(screen.getByText(/queued behind a turn that ended early/)).toBeTruthy()
    unmount()
    render(<SystemEvent subtype={SYSTEM_SUBTYPE.UNDELIVERED_INPUT}
                        reason={UNDELIVERED_REASON.STOPPED} message="two" />)
    expect(screen.getByText('This message was not sent')).toBeTruthy()
    expect(screen.getByText(/queued when the turn was stopped/)).toBeTruthy()
    expect(screen.queryByText(/typed while the session was starting/)).toBeNull()
  })

  it("keeps the terminal's cautious wording without a reason", () => {
    render(<SystemEvent subtype={SYSTEM_SUBTYPE.UNDELIVERED_INPUT} message="lost prompt" />)
    expect(screen.getByText('This message may not have reached the agent')).toBeTruthy()
    expect(screen.getByText(/typed while the session was starting/)).toBeTruthy()
  })

  it('keeps its reason through a history row and a live block, so a reload says the same', () => {
    const evt = { type: 'system', subtype: SYSTEM_SUBTYPE.UNDELIVERED_INPUT,
                  reason: UNDELIVERED_REASON.QUEUED, message: 'hello' }
    expect(eventToBlock(evt)).toMatchObject({ type: 'system', reason: UNDELIVERED_REASON.QUEUED })
    expect(liveBlockToMessageBlock(evt)).toMatchObject({ type: 'system', reason: UNDELIVERED_REASON.QUEUED })
  })
})

describe('the machine-reconnecting line', () => {
  it('names why the turn was not sent', () => {
    render(<SystemEvent subtype={SYSTEM_SUBTYPE.MACHINE_RECONNECTING}
                        message="Not sent: the machine running this chat is reconnecting. Send it again once it is back." />)
    expect(screen.getByTestId('machine-reconnecting')).toBeTruthy()
    expect(screen.getByText('The machine is reconnecting')).toBeTruthy()
    expect(screen.getByText(/Send it again once it is back/)).toBeTruthy()
  })
})
