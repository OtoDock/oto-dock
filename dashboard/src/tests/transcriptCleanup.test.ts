/**
 * Render-side cleanup of persisted user rows — injected time preludes and
 * slash-command transcript noise. Fixtures mirror real chat_messages rows
 * (interactive tailer output); the stored data itself is never modified.
 */
import { readFileSync } from 'node:fs'
import path from 'node:path'
import { fileURLToPath } from 'node:url'
import { describe, it, expect } from 'vitest'
import { stripInjectedPreludes, cleanUserMessageText, stripPlatformWrappers, removeTagBlocks } from '@/lib/transcriptCleanup'
import { dbMessagesToDisplay } from '@/lib/messageBlocks'

const STAMP = '[Current time: Tuesday, July 07, 2026 17:23 (5:23 PM) Europe/Athens (UTC+03:00)]'
const here = path.dirname(fileURLToPath(import.meta.url))

// The platform's wrappers, pinned case by case against the proxy's title
// stripper through the one fixture both sides read
// (proxy/tests/fixtures/chat_wrappers.json): `stripped` is what a reader
// shows of the row, null when the row is not drawn.
describe('the platform wrappers fixture', () => {
  const { cases } = JSON.parse(
    readFileSync(path.resolve(here, '../../../proxy/tests/fixtures/chat_wrappers.json'), 'utf8'),
  ) as { cases: { name: string; input: string; stripped: string | null }[] }
  it('has every shape', () => {
    expect(cases.length).toBeGreaterThanOrEqual(20)
  })
  it.each(cases.map((c) => [c.name, c] as const))('%s', (_name, c) => {
    expect(cleanUserMessageText(c.input)).toBe(c.stripped)
  })
})

// Every viewer runs the stripper on every person's row, a shared chat's
// rows included: a quadratic pattern made a 40 KB row take seconds.
describe('a crafted row cleans in linear time', () => {
  const MB = 1_000_000
  it.each([
    ['a paste open then 1 MB of spaces', '<pasted_content>' + ' '.repeat(MB) + 'x'],
    ['unclosed reminders', '<system-reminder>'.repeat(Math.floor(MB / 17))],
    ['unclosed command tags', '<command-name>'.repeat(Math.floor(MB / 14))],
    ['plain long text', 'word '.repeat(MB / 5)],
    ['unterminated close tags', '<pasted_content>' + '</pasted_content'.repeat(MB / 16)],
    ['stacked preludes', '[Current time: x] '.repeat(Math.floor(MB / 18)) + 'hi'],
    ['a run of image marks', '[Image #1] '.repeat(Math.floor(MB / 11)) + 'x'],
  ])('%s', (_name, row) => {
    const started = performance.now()
    cleanUserMessageText(row)
    expect(performance.now() - started).toBeLessThan(1000)
  })

  it('removes tag blocks as the lazy pattern would', () => {
    expect(removeTagBlocks('a<x>1</x>b<y>2</y>c<x>open', ['x', 'y'])).toBe('abc<x>open')
    expect(removeTagBlocks('<y>no close <x>in</x> after', ['x', 'y'])).toBe('<y>no close  after')
    expect(removeTagBlocks('<x><y></x></y>', ['x', 'y'])).toBe('</y>')
  })
})

describe('stripPlatformWrappers', () => {
  it('drops a leading, closed wrapper and keeps everything else', () => {
    expect(stripPlatformWrappers('[DELEGATED_WORK]x[/DELEGATED_WORK]\n\n[TASK_OUTPUT_DIR]y[/TASK_OUTPUT_DIR]\n\nthe task'))
      .toBe('the task')
    expect(stripPlatformWrappers('[TASK_OUTPUT_DIR]y[/TASK_OUTPUT_DIR] [DELEGATED_WORK]x[/DELEGATED_WORK] odd order'))
      .toBe('[DELEGATED_WORK]x[/DELEGATED_WORK] odd order')
    expect(stripPlatformWrappers('[DELEGATED_WORK] open only')).toBe('[DELEGATED_WORK] open only')
  })
})

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

describe('stripInjectedPreludes on a task run', () => {
  const parallel =
    "[Parallel activity — for awareness only, no action needed: sibling sessions: 'Fix [bug] 2' (generating). " +
    'Check one only if your work depends on it (peek_session / get_task_result).]'

  it('strips the time and parallel-activity lines of a first prompt', () => {
    const text = `[Current time: Friday, October 09, 2026 06:00 (6:00 AM) UTC (UTC+00:00)]\n\n${parallel}\n\nsummarise the inbox`
    expect(stripInjectedPreludes(text)).toBe('summarise the inbox')
  })

  it('strips them when a satellite single-lined the prompt', () => {
    const text = `[Current time: Friday, October 09, 2026 06:00 (6:00 AM) UTC (UTC+00:00)] ${parallel} summarise the inbox`
    expect(stripInjectedPreludes(text)).toBe('summarise the inbox')
  })

  it('leaves a mid-message quote of the shape alone', () => {
    const text = `see ${parallel}`
    expect(stripInjectedPreludes(text)).toBe(text)
  })
})

describe('stripInjectedPreludes on a concluded meeting note', () => {
  const note =
    '[Meeting "q3 [margins]" (mtg-0123456789ab) ended 2026-10-16 01:00 UTC after 3 turns, with Head, Alpha. ' +
    'Summary: keep 12 %. ] then The person sees the whole meeting in this chat; you have only this note.]'

  it('strips the note behind the time stamp, multi-line or single-lined', () => {
    expect(stripInjectedPreludes(`${STAMP}\n\n${note}\n\nwhat did they decide?`)).toBe('what did they decide?')
    expect(stripInjectedPreludes(`${STAMP} ${note} what did they decide?`)).toBe('what did they decide?')
  })

  it('leaves a quote of its tail mid-message alone', () => {
    const text = 'say "you have only this note.]" back'
    expect(stripInjectedPreludes(text)).toBe(text)
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

  it('strips a leading duplex-context system-reminder block', () => {
    // ws/duplex_attach.py::_build_prompt shape — the block is prepended to
    // the FIRST spoken turn after each attach; the interactive tailer
    // persists the PTY-typed prompt verbatim.
    const row =
      '<system-reminder>\nYou are in a live spoken conversation. Keep ' +
      'replies short.\n</system-reminder>\n\nwhat is on my calendar today'
    expect(cleanUserMessageText(row)).toBe('what is on my calendar today')
  })

  it('hides a row that is ONLY a system-reminder block', () => {
    const row = '<system-reminder>\ncontext only\n</system-reminder>'
    expect(cleanUserMessageText(row)).toBeNull()
  })

  it('strips a time stamp that sat between the block and the text', () => {
    const row =
      `<system-reminder>\nctx\n</system-reminder>\n\n${STAMP}\n\nhello`
    expect(cleanUserMessageText(row)).toBe('hello')
  })

  it('keeps a message that quotes a system-reminder mid-text', () => {
    const text =
      'why does <system-reminder>this</system-reminder> show in my history?'
    expect(cleanUserMessageText(text)).toBe(text)
  })

  it('keeps a message starting with an unclosed system-reminder mention', () => {
    const text = '<system-reminder> is a tag the platform injects'
    expect(cleanUserMessageText(text)).toBe(text)
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

  it("strips the TUI's paste tags only when they wrap the whole row", () => {
    expect(cleanUserMessageText(`<pasted_content id="12">\n${STAMP}\n\nhello\n</pasted_content>`)).toBe('hello')
    const open = '<pasted_content id="12">\nno close tag'
    expect(cleanUserMessageText(open)).toBe(open)
    const close = 'no open tag\n</pasted_content>'
    expect(cleanUserMessageText(close)).toBe(close)
  })

  it('keeps a row that mentions the report envelope mid-text', () => {
    const text = 'the callback starts with [BACKGROUND_TASK_RESULT], then the output'
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
