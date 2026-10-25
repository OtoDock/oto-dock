/**
 * The turn-ended card (`system` row, subtype `turn_ended`): the ending's
 * line with the proxy's old instruction dropped, the card's own sentence,
 * and its button: Resume once the turn had started, Send again when nothing
 * ran (lib/messageBlocks.sendAgainAction), none for a decline or a limit.
 */
import { describe, it, expect, vi } from 'vitest'
import { render, screen, fireEvent } from '@testing-library/react'
import SystemEvent from '@/components/chat/SystemEvent'
import { SYSTEM_SUBTYPE, type SystemFrame } from '@/api/wireEvents'
import {
  eventToBlock, lastTurnPrompt, liveBlockToMessageBlock, RESUME_TEXT, sendAgainAction, sendAgainKind, turnHadWork,
} from '@/lib/messageBlocks'
import type { DisplayMessage } from '@/components/chat/types'

describe('the turn-ended card', () => {
  it('offers Send again when nothing ran, and says what the button does', () => {
    const onSendAgain = vi.fn()
    render(<SystemEvent subtype={SYSTEM_SUBTYPE.TURN_ENDED} reason="exited" sendAgain="again"
                        message="⚠ The engine's process exited before it answered (exit code 137). Send the message again, the conversation continues where it left off."
                        onSendAgain={onSendAgain} />)
    expect(screen.getByText('This turn ended early')).toBeTruthy()
    expect(screen.getByText(/exit code 137/).textContent).not.toMatch(/Send the message again/)
    expect(screen.getByText('Send again sends your message again.')).toBeTruthy()
    fireEvent.click(screen.getByRole('button', { name: 'Send again' }))
    expect(onSendAgain).toHaveBeenCalledTimes(1)
  })

  it('offers Resume once the turn had started, the proxy instruction dropped', () => {
    const onSendAgain = vi.fn()
    render(<SystemEvent subtype={SYSTEM_SUBTYPE.TURN_ENDED} reason="lost" sendAgain="resume"
                        message="⚠ The connection to the machine running this chat was lost mid-turn. Send the message again once the machine is back."
                        onSendAgain={onSendAgain} />)
    expect(screen.getByText(/lost mid-turn\.$/)).toBeTruthy()
    expect(screen.queryByText(/once the machine is back/)).toBeNull()
    expect(screen.getByText('Resume asks the agent to carry on from where it stopped.')).toBeTruthy()
    fireEvent.click(screen.getByRole('button', { name: 'Resume' }))
    expect(onSendAgain).toHaveBeenCalledTimes(1)
    expect(screen.queryByRole('button', { name: 'Send again' })).toBeNull()
  })

  it('shows the line whole and no button without a kind', () => {
    render(<SystemEvent subtype={SYSTEM_SUBTYPE.TURN_ENDED} reason="declined"
                        message="⚠ The model's safety classifier declined this turn." onSendAgain={() => {}} />)
    expect(screen.queryByRole('button')).toBeNull()
    render(<SystemEvent subtype={SYSTEM_SUBTYPE.TURN_ENDED} reason="stopped"
                        message="⚠ The engine was stopped before this message ran. Send it again." />)
    expect(screen.getByText(/Send it again\./)).toBeTruthy()
    expect(screen.queryByRole('button')).toBeNull()
  })

  it('keeps its reason through a history row and a live block', () => {
    const evt: SystemFrame = { type: 'system', subtype: SYSTEM_SUBTYPE.TURN_ENDED, reason: 'silent', message: 'gone quiet' }
    expect(eventToBlock(evt)).toMatchObject({ type: 'system', subtype: 'turn_ended', reason: 'silent', message: 'gone quiet' })
    expect(liveBlockToMessageBlock(evt)).toMatchObject({ type: 'system', subtype: 'turn_ended', reason: 'silent' })
  })
})

const user = (id: string, text: string): DisplayMessage =>
  ({ id, role: 'user', blocks: [{ type: 'text', content: text }] }) as unknown as DisplayMessage
const steer = (id: string, text: string): DisplayMessage => ({ ...user(id, text), steered: true })
const answer = (id: string, text: string): DisplayMessage =>
  ({ id, role: 'assistant', blocks: [{ type: 'text', content: text }] }) as unknown as DisplayMessage
const finished = (id: string, text: string): DisplayMessage =>
  ({ id, role: 'assistant', blocks: [{ type: 'text', content: text }, { type: 'metadata', costUsd: 0, durationMs: 1 }] }) as unknown as DisplayMessage
const ended = (id: string, reason = 'silent', work: object[] = []): DisplayMessage =>
  ({ id, role: 'assistant', blocks: [...work, { type: 'system', subtype: SYSTEM_SUBTYPE.TURN_ENDED,
                                                reason, message: reason }] }) as unknown as DisplayMessage
const tool = { type: 'tool', name: 'Read', toolId: 't1', summary: 'a file', status: 'done' }

describe("the card's button", () => {
  it('is Resume once the turn had started, with one short message', () => {
    const msgs = [user('u1', 'write the report'), ended('a1', 'lost', [{ type: 'text', content: 'Starting' }])]
    expect(turnHadWork(msgs, 'a1')).toBe(true)
    expect(sendAgainAction(msgs, 'a1')).toMatchObject({ kind: 'resume', text: RESUME_TEXT })
    expect(sendAgainKind(msgs, msgs[1])).toBe('resume')
    expect(sendAgainKind(msgs, msgs[0])).toBeUndefined()
  })

  it("counts an earlier unfinished bubble's work and passes over a steer", () => {
    const msgs = [user('u0', 'hello'), finished('a0', 'hi'), user('u1', 'prompt'), answer('a1', 'half'),
                  steer('u2', 'and this'), ended('a2', 'exited')]
    expect(turnHadWork(msgs, 'a2')).toBe(true)
    expect(sendAgainAction(msgs, 'a2')?.kind).toBe('resume')
  })

  it('is Send again when nothing ran, with the turn verbatim', () => {
    const msgs = [user('u0', 'hello'), finished('a0', 'hi'), user('u1', 'first'), user('u2', 'second'), ended('a1', 'exited')]
    expect(turnHadWork(msgs, 'a1')).toBe(false)
    expect(sendAgainAction(msgs, 'a1')).toMatchObject({ kind: 'again', text: 'first\n\nsecond' })
  })

  it("does not read the previous turn's answer as work", () => {
    const msgs = [user('u0', 'one'), finished('a0', 'done'), user('u1', 'two'), ended('a1', 'silent')]
    expect(turnHadWork(msgs, 'a1')).toBe(false)
  })

  it('is always Send again for a message that met a stopped engine', () => {
    const msgs = [user('u1', 'go'), ended('a1', 'stopped', [{ type: 'text', content: 'partial' }])]
    expect(sendAgainAction(msgs, 'a1')?.kind).toBe('again')
  })

  it('after a restart: Resume with work, nothing without; nothing for a decline', () => {
    expect(sendAgainAction([user('u1', 'go'), ended('a1', 'restarted', [tool])], 'a1')?.kind).toBe('resume')
    expect(sendAgainAction([user('u1', 'go'), ended('a1', 'restarted')], 'a1')).toBeNull()
    expect(sendAgainAction([user('u1', 'go'), ended('a1', 'declined', [tool])], 'a1')).toBeNull()
  })

  it('carries the saved attachments after an ending that lost the session, not after an error', () => {
    const withFile = {
      id: 'u1', role: 'user', blocks: [
        { type: 'file_attachments', files: [{ name: 'r.pdf', path: 'users/me/r.pdf' }] },
        { type: 'text', content: 'read this' },
      ],
    } as unknown as DisplayMessage
    const lost = sendAgainAction([withFile, ended('a1', 'lost', [tool])], 'a1')
    expect(lost?.kind).toBe('resume')
    expect(lost?.files.map((f) => f.uploadedPath)).toEqual(['users/me/r.pdf'])
    const error = sendAgainAction([withFile, ended('a1', 'error', [tool])], 'a1')
    expect(error?.kind).toBe('resume')
    expect(error?.files).toEqual([])
  })

  it("an earlier card's turn is its own", () => {
    const msgs = [user('u1', 'first try'), ended('a1', 'exited'), user('u2', 'other'), finished('a2', 'done')]
    expect(sendAgainAction(msgs, 'a1')).toMatchObject({ kind: 'again', text: 'first try' })
  })
})

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
