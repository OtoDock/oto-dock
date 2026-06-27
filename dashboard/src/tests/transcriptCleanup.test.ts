/**
 * Render-side cleanup of persisted user rows — injected time preludes and
 * slash-command transcript noise. Fixtures mirror real chat_messages rows
 * (interactive tailer output); the stored data itself is never modified.
 */
import { describe, it, expect } from 'vitest'
import { stripInjectedPreludes, cleanUserMessageText } from '@/lib/transcriptCleanup'
import { dbMessagesToDisplay } from '@/lib/messageBlocks'

const STAMP = '[Current time: Tuesday, July 07, 2026 17:23 (5:23 PM) Europe/Athens (UTC+03:00)]'

describe('stripInjectedPreludes', () => {
  it('strips a leading time stamp + blank line', () => {
    expect(stripInjectedPreludes(`${STAMP}\n\nhello there`)).toBe('hello there')
  })

  it('folds stacked stamps (re-warm injections)', () => {
    expect(stripInjectedPreludes(`${STAMP}\n\n${STAMP}\n\nhi`)).toBe('hi')
  })

  it('leaves mid-message mentions alone', () => {
    const text = `see the ${STAMP} header`
    expect(stripInjectedPreludes(text)).toBe(text)
  })

  it('no-ops on clean text', () => {
    expect(stripInjectedPreludes('plain prompt')).toBe('plain prompt')
  })
})

describe('cleanUserMessageText', () => {
  it('hides a pure slash-command record', () => {
    const row = '<command-name>/model</command-name>\n            <command-message>model</command-message>\n            <command-args></command-args>'
    expect(cleanUserMessageText(row)).toBeNull()
  })

  it('hides local command output', () => {
    expect(cleanUserMessageText('<local-command-stdout>Login successful</local-command-stdout>')).toBeNull()
  })

  it('hides a /context report', () => {
    const row = '## Context Usage\n\n**Model:** claude-fable-5  \n**Tokens:** 621.8k / 1m (62%)\n\n| Category | Tokens |\n|---|---|'
    expect(cleanUserMessageText(row)).toBeNull()
  })

  it('keeps a legit message that merely quotes a tag', () => {
    const text = 'why does <command-name> appear in my transcript?'
    expect(cleanUserMessageText(text)).toBe(text)
  })

  it('keeps pasted markdown that only looks similar', () => {
    const text = '## Context Usage\n\nmy own notes about token budgets'
    expect(cleanUserMessageText(text)).toBe(text)
  })

  it('hides a bare stamp with no user text', () => {
    expect(cleanUserMessageText(`${STAMP}\n\n`)).toBeNull()
  })

  it('strips the stamp off a real prompt', () => {
    expect(cleanUserMessageText(`${STAMP}\n\ntest`)).toBe('test')
  })

  it('hides a harness-injected task-notification row', () => {
    const row =
      '<task-notification>\n<task-id>bhfa2mjno</task-id>\n' +
      '<status>completed</status>\n' +
      '<summary>Background command "build" completed (exit code 0)</summary>\n' +
      '</task-notification>'
    expect(cleanUserMessageText(row)).toBeNull()
  })

  it('keeps a message that pastes a task-notification mid-text', () => {
    const text =
      'look: <task-notification>x</task-notification> — why does this show?'
    expect(cleanUserMessageText(text)).toBe(text)
  })

  it('keeps a message starting with the tag but never closing it', () => {
    const text = '<task-notification> is a tag the harness uses'
    expect(cleanUserMessageText(text)).toBe(text)
  })
})

describe('dbMessagesToDisplay integration', () => {
  const base = { event_type: '', event_data: '', created_at: '2026-07-07T00:00:00+00:00' }

  it('drops pure-noise user rows and strips preludes from real ones', () => {
    const msgs = dbMessagesToDisplay(
      [
        { ...base, id: 1, role: 'user', content: '<command-name>/login</command-name>\n<command-message>login</command-message>\n<command-args></command-args>' },
        { ...base, id: 2, role: 'user', content: '<local-command-stdout>Login successful</local-command-stdout>' },
        { ...base, id: 3, role: 'user', content: `${STAMP}\n\nreal question` },
        { ...base, id: 4, role: 'assistant', content: 'real answer' },
      ],
      undefined,
    )
    expect(msgs).toHaveLength(2)
    expect(msgs[0].role).toBe('user')
    expect(msgs[0].blocks).toEqual([{ type: 'text', content: 'real question' }])
    expect(msgs[1].role).toBe('assistant')
  })

  it('keeps a noise-text row alive when it carries attachments', () => {
    const msgs = dbMessagesToDisplay(
      [{
        ...base, id: 5, role: 'user',
        content: '<local-command-stdout>x</local-command-stdout>',
        event_data: JSON.stringify({ images: [{ name: 'pic.png' }] }),
      }],
      undefined,
    )
    expect(msgs).toHaveLength(1)
    expect(msgs[0].blocks.map((b) => b.type)).toEqual(['image_attachments'])
  })
})
