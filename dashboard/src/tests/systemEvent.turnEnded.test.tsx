/**
 * The turn-ended card (`system` row, subtype `turn_ended`): the ending's
 * line, and Send again for the endings a verbatim re-send can answer (an
 * error, an exit, a silence, a lost machine, a message that met a stopped
 * engine), never for a decline or a limit.
 */
import { describe, it, expect, vi } from 'vitest'
import { render, screen, fireEvent } from '@testing-library/react'
import SystemEvent from '@/components/chat/SystemEvent'
import { SYSTEM_SUBTYPE } from '@/api/wireEvents'
import { eventToBlock, lastTurnPrompt, liveBlockToMessageBlock, sendAgainPrompt } from '@/lib/messageBlocks'
import type { DisplayMessage } from '@/components/chat/types'

describe('the turn-ended card', () => {
  it('offers Send again for an error ending', () => {
    const onSendAgain = vi.fn()
    render(<SystemEvent subtype={SYSTEM_SUBTYPE.TURN_ENDED} reason="exited"
                        message="⚠ The engine's process exited before it answered (exit code 137)."
                        onSendAgain={onSendAgain} />)
    expect(screen.getByText('This turn ended early')).toBeTruthy()
    expect(screen.getByText(/exit code 137/)).toBeTruthy()
    fireEvent.click(screen.getByRole('button', { name: 'Send again' }))
    expect(onSendAgain).toHaveBeenCalledTimes(1)
  })

  it('offers Send again for a message that met a stopped engine', () => {
    const onSendAgain = vi.fn()
    render(<SystemEvent subtype={SYSTEM_SUBTYPE.TURN_ENDED} reason="stopped"
                        message="⚠ The engine was stopped before this message ran. Send it again."
                        onSendAgain={onSendAgain} />)
    fireEvent.click(screen.getByRole('button', { name: 'Send again' }))
    expect(onSendAgain).toHaveBeenCalledTimes(1)
  })

  it('offers no re-send for a decline or a limit', () => {
    render(<SystemEvent subtype={SYSTEM_SUBTYPE.TURN_ENDED} reason="declined"
                        message="⚠ The model's safety classifier declined this turn." onSendAgain={() => {}} />)
    expect(screen.queryByRole('button', { name: 'Send again' })).toBeNull()
    render(<SystemEvent subtype={SYSTEM_SUBTYPE.TURN_ENDED} reason="limit"
                        message="⚠ Usage limit reached." onSendAgain={() => {}} />)
    expect(screen.queryByRole('button', { name: 'Send again' })).toBeNull()
  })

  it('keeps its reason through a history row and a live block', () => {
    const evt = { type: 'system', subtype: SYSTEM_SUBTYPE.TURN_ENDED, reason: 'silent', message: 'gone quiet' }
    expect(eventToBlock(evt)).toMatchObject({ type: 'system', subtype: 'turn_ended', reason: 'silent', message: 'gone quiet' })
    expect(liveBlockToMessageBlock(evt)).toMatchObject({ type: 'system', subtype: 'turn_ended', reason: 'silent' })
  })
})

const user = (id: string, text: string): DisplayMessage =>
  ({ id, role: 'user', blocks: [{ type: 'text', content: text }] }) as unknown as DisplayMessage
const answer = (id: string, text: string): DisplayMessage =>
  ({ id, role: 'assistant', blocks: [{ type: 'text', content: text }] }) as unknown as DisplayMessage
const ended = (id: string): DisplayMessage =>
  ({ id, role: 'assistant', blocks: [{ type: 'system', subtype: SYSTEM_SUBTYPE.TURN_ENDED,
                                      reason: 'silent', message: 'silent' }] }) as unknown as DisplayMessage

describe("Send again's prompt", () => {
  it('takes every message of the turn that ended: the batch and a steer', () => {
    const msgs = [user('u1', 'hello'), answer('a1', 'hi'), user('u2', 'first'),
                  user('u3', 'second'), ended('a2')]
    expect(lastTurnPrompt(msgs)?.text).toBe('first\n\nsecond')
  })

  it('stops at the card of an earlier turn that ended short', () => {
    const msgs = [user('u1', 'story'), answer('a1', 'once upon'), user('u2', 'sequel'),
                  ended('a2'), user('u3', 'sequel'), user('u4', 'and a title'), ended('a3')]
    expect(lastTurnPrompt(msgs)?.text).toBe('sequel\n\nand a title')
  })

  it("an earlier card's slice re-sends that card's own turn", () => {
    const msgs = [user('u1', 'first try'), ended('a1'), user('u2', 'other'), answer('a2', 'done')]
    expect(sendAgainPrompt(msgs, 'a1')?.text).toBe('first try')
  })

  it("re-sends a live bubble's fresh photo and a row's saved one", () => {
    const withPhotos = {
      id: 'u1', role: 'user', blocks: [
        { type: 'text', content: 'look' },
        { type: 'image_attachments', images: ['data:image/png;base64,AAAA', 'saved.png'],
          paths: [null, 'users/me/workspace/saved.png'] },
      ],
    } as unknown as DisplayMessage
    const again = lastTurnPrompt([withPhotos, ended('a1')])
    expect(again?.images).toEqual([
      { id: 'again-img-0', base64: 'data:image/png;base64,AAAA', name: 'photo-1' },
      { id: 'again-img-users/me/workspace/saved.png', path: 'users/me/workspace/saved.png', name: 'saved.png' },
    ])
  })

  it('re-sends a file by its saved path, and one with no path is left out', () => {
    const withFiles = {
      id: 'u1', role: 'user', blocks: [
        { type: 'file_attachments', files: [
          { name: 'report.pdf', path: 'users/me/workspace/uploads/report.pdf' },
          { name: 'still-uploading.zip' },
        ] },
        { type: 'text', content: 'summarise this' },
      ],
    } as unknown as DisplayMessage
    const again = lastTurnPrompt([user('u0', 'hi'), answer('a0', 'hello'), withFiles, ended('a1')])
    expect(again?.text).toBe('summarise this')
    expect(again?.files).toEqual([{
      id: 'again-file-users/me/workspace/uploads/report.pdf', name: 'report.pdf', size: 0,
      uploadedPath: 'users/me/workspace/uploads/report.pdf',
    }])
    expect(again?.images).toEqual([])
  })
})
