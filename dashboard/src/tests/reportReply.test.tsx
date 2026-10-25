/**
 * Reporting an agent's reply: the flag sits in the footer of a complete
 * agent reply on a chat that exists, never under a person's message or a
 * reply still being written. The dialog says what goes to whom, asks a
 * reason (an optional note), names the reply by its first stored row (a
 * streamed reply is found among the chat's newest rows by its text and its
 * place among equal replies, never guessed), then thanks the person in place.
 * Cancel and Escape send nothing, and the focus comes back to the flag.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { act, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'

import ChatMessages from '@/components/chat/ChatMessages'
import type { DisplayMessage, MessageBlock } from '@/components/chat/types'
import { REPLY_REPORT_NOTE_MAX, REPLY_REPORT_REASONS } from '@/api/replyReports'
import { ApiError } from '@/lib/http/apiJson'
import { findStoredReply, storedRowId } from '@/lib/messageBlocks'

type Page = { messages: Array<Record<string, unknown>>; has_more: boolean }
const api = vi.hoisted(() => ({
  reportReply: vi.fn(async (_chatId: string, _body: unknown) => {}),
  fetchRowsPage: vi.fn(async (_chatId: string, _before?: number): Promise<Page> => ({ messages: [], has_more: false })),
}))
vi.mock('@/api/replyReports', async (importOriginal) => ({
  ...(await importOriginal<typeof import('@/api/replyReports')>()),
  reportReply: api.reportReply,
  fetchRowsPage: api.fetchRowsPage,
}))

class ObserverStub {
  observe() {}
  unobserve() {}
  disconnect() {}
}
vi.stubGlobal('ResizeObserver', ObserverStub)
vi.stubGlobal('IntersectionObserver', ObserverStub)

const META: MessageBlock = { type: 'metadata', costUsd: 0, durationMs: 1200, costBilled: false }

function reply(id: string, text: string, done = true): DisplayMessage {
  return {
    id, role: 'assistant', createdAt: '2026-10-21T00:00:00+00:00',
    blocks: done ? [{ type: 'text', content: text }, META] : [{ type: 'text', content: text }],
  }
}

function renderChat(messages: DisplayMessage[], { chatId = 'chat-1', streaming = false } = {}) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <QueryClientProvider client={qc}>
      <ChatMessages messages={messages} agentName="dev" chatId={chatId || undefined}
        streaming={streaming} onPermissionRespond={() => {}} />
    </QueryClientProvider>,
  )
}

const flags = () => screen.queryAllByRole('button', { name: 'Report this reply' })

async function openAndSend(reason: string, note = '', which = 0) {
  fireEvent.click(flags()[which])
  const dialog = await screen.findByTestId('report-reply-dialog')
  const send = within(dialog).getByRole('button', { name: 'Send report' })
  expect(send).toBeDisabled()
  fireEvent.click(within(dialog).getByRole('radio', { name: reason }))
  if (note) fireEvent.change(within(dialog).getByLabelText('Note, optional'), { target: { value: note } })
  await waitFor(() => expect(send).toBeEnabled())
  await act(async () => { fireEvent.click(send) })
  return dialog
}

async function sendHarmfulInOpenDialog() {
  const dialog = await screen.findByTestId('report-reply-dialog')
  fireEvent.click(within(dialog).getByRole('radio', { name: 'Harmful or offensive' }))
  await act(async () => { fireEvent.click(within(dialog).getByRole('button', { name: 'Send report' })) })
  return dialog
}

const row = (id: number, role: string, content: string, extra: Record<string, unknown> = {}) =>
  ({ id, role, content, created_at: 't', ...extra })

beforeEach(() => {
  api.reportReply.mockReset()
  api.reportReply.mockImplementation(async () => {})
  api.fetchRowsPage.mockReset()
  api.fetchRowsPage.mockImplementation(async () => ({ messages: [], has_more: false }))
})
afterEach(() => {
  vi.clearAllMocks()
  vi.useRealTimers()
})

describe('the report flag', () => {
  it('sits under a complete agent reply', () => {
    renderChat([reply('db-7', 'An answer.')])
    expect(flags()).toHaveLength(1)
  })

  it('never under a person or a reply still being written, nor on a chat with no id', () => {
    const person: DisplayMessage = {
      id: 'db-1', role: 'user', createdAt: '2026-10-21T00:00:00+00:00',
      blocks: [{ type: 'text', content: 'A question.' }],
    }
    const prompt: DisplayMessage = { ...person, id: 'db-2', agentSlug: 'boss' }
    renderChat([person, prompt, reply('stream-9', 'Still writing', false)], { streaming: true })
    expect(flags()).toHaveLength(0)
  })

  it('never on a chat that has no id yet', () => {
    renderChat([reply('db-7', 'An answer.')], { chatId: '' })
    expect(flags()).toHaveLength(0)
  })
})

describe('the report dialog', () => {
  it('sends a stored reply by its first row and thanks the person', async () => {
    renderChat([reply('db-evt-41', 'An answer.')])
    const dialog = await openAndSend('Wrong or misleading', '  It made this up.  ')
    expect(api.reportReply).toHaveBeenCalledWith('chat-1', { message_id: 41, reason: 'misleading', note: 'It made this up.' })
    expect(api.fetchRowsPage).not.toHaveBeenCalled()
    expect(within(dialog).getByRole('status')).toHaveTextContent('The admins of this OtoDock have your report.')
    fireEvent.click(within(dialog).getByRole('button', { name: 'Done' }))
    expect(screen.getByRole('button', { name: 'Reported' })).toBeInTheDocument()
  })

  it('says what goes to whom before anything is sent', async () => {
    renderChat([reply('db-7', 'An answer.')])
    fireEvent.click(flags()[0])
    const dialog = await screen.findByTestId('report-reply-dialog')
    expect(dialog).toHaveTextContent('The admins of this OtoDock will receive this report with your name.')
    expect(within(dialog).getAllByRole('radio')).toHaveLength(4)
    const described = document.getElementById(dialog.getAttribute('aria-describedby') || '')
    expect(described).toHaveTextContent('The admins of this OtoDock will receive this report with your name.')
  })

  it('sends nothing on Cancel or Escape, and the focus returns to the flag after Done', async () => {
    renderChat([reply('db-7', 'An answer.')])
    fireEvent.click(flags()[0])
    let dialog = await screen.findByTestId('report-reply-dialog')
    fireEvent.click(within(dialog).getByRole('button', { name: 'Cancel' }))
    await waitFor(() => expect(screen.queryByTestId('report-reply-dialog')).toBeNull())
    fireEvent.click(flags()[0])
    dialog = await screen.findByTestId('report-reply-dialog')
    fireEvent.keyDown(dialog, { key: 'Escape' })
    await waitFor(() => expect(screen.queryByTestId('report-reply-dialog')).toBeNull())
    expect(api.reportReply).not.toHaveBeenCalled()

    const flag = flags()[0]
    flag.focus()
    fireEvent.click(flag)
    dialog = await sendHarmfulInOpenDialog()
    fireEvent.click(within(dialog).getByRole('button', { name: 'Done' }))
    await waitFor(() => expect(screen.queryByTestId('report-reply-dialog')).toBeNull())
    expect(document.activeElement).toBe(screen.getByRole('button', { name: 'Reported' }))
  })

  it('says it is finding a streamed reply, and a failed read says so', async () => {
    let fail: (e: unknown) => void = () => {}
    api.fetchRowsPage.mockImplementation(() => new Promise((_resolve, reject) => { fail = reject }))
    renderChat([reply('stream-1', 'An answer.')])
    fireEvent.click(flags()[0])
    const dialog = await screen.findByTestId('report-reply-dialog')
    expect(within(dialog).getByRole('status')).toHaveTextContent('Finding the reply…')
    await act(async () => { fail(new ApiError(0, 'The server did not answer.', undefined, null)) })
    expect(within(dialog).getByRole('alert')).toHaveTextContent('The server did not answer.')
  })

  it('finds a streamed reply among the newest rows by its text', async () => {
    api.fetchRowsPage.mockImplementation(async () => ({
      messages: [row(50, 'user', 'Q'), row(51, 'assistant', 'An  answer.'), row(52, 'user', 'Q2'),
        row(53, 'assistant', 'Another.')],
      has_more: false,
    }))
    renderChat([reply('stream-1', 'An answer.')])
    await openAndSend('Harmful or offensive')
    expect(api.fetchRowsPage).toHaveBeenCalledWith('chat-1', undefined)
    expect(api.reportReply).toHaveBeenCalledWith('chat-1', { message_id: 51, reason: 'harmful', note: '' })
  })

  it('reads an older page when the page boundary cuts the reply', async () => {
    api.fetchRowsPage.mockImplementation(async (_c: string, before?: number) => (before
      ? { messages: [row(60, 'user', 'Q'), row(61, 'assistant', 'First part.')], has_more: false }
      : { messages: [row(62, 'event', '', { event_type: 'tool_use', event_data: '{}' }), row(63, 'assistant', 'Second part.')],
          has_more: true }))
    renderChat([reply('stream-1', 'First part.\n\nSecond part.')])
    await openAndSend('Wrong or misleading')
    expect(api.fetchRowsPage).toHaveBeenLastCalledWith('chat-1', 62)
    expect(api.reportReply).toHaveBeenCalledWith('chat-1', { message_id: 61, reason: 'misleading', note: '' })
  })

  it('does not count a later equal reply that is still being written', async () => {
    api.fetchRowsPage.mockImplementation(async () => ({
      messages: [row(80, 'user', 'a'), row(81, 'assistant', 'Done.'), row(82, 'user', 'b')],
      has_more: false,
    }))
    renderChat([reply('stream-1', 'Done.'), reply('stream-2', 'Done.', false)])
    await openAndSend('Something else', '', 0)
    expect(api.reportReply).toHaveBeenCalledWith('chat-1', { message_id: 81, reason: 'other', note: '' })
  })

  it('tells twins apart by their place from the end', async () => {
    api.fetchRowsPage.mockImplementation(async () => ({
      messages: [row(70, 'user', 'a'), row(71, 'assistant', 'Same.'), row(72, 'user', 'b'), row(73, 'assistant', 'Same.')],
      has_more: false,
    }))
    renderChat([reply('stream-1', 'Same.'), reply('stream-2', 'Same.')])
    await openAndSend('Something else', '', 0)
    expect(api.reportReply).toHaveBeenCalledWith('chat-1', { message_id: 71, reason: 'other', note: '' })
  })

  it('refuses to guess when the streamed reply is not stored', async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true })
    api.fetchRowsPage.mockImplementation(async () => ({ messages: [row(53, 'assistant', 'Another.')], has_more: false }))
    renderChat([reply('stream-1', 'An answer.')])
    fireEvent.click(flags()[0])
    const dialog = await screen.findByTestId('report-reply-dialog')
    await act(async () => { await vi.advanceTimersByTimeAsync(4000) })
    expect(within(dialog).getByRole('alert')).toHaveTextContent('Reload the chat, then report this reply.')
    fireEvent.click(within(dialog).getByRole('radio', { name: 'Something else' }))
    expect(within(dialog).getByRole('button', { name: 'Send report' })).toBeDisabled()
    expect(api.fetchRowsPage.mock.calls.length).toBeGreaterThan(1)
    expect(api.reportReply).not.toHaveBeenCalled()
  })

  it("shows the server's sentence when the report is refused", async () => {
    api.reportReply.mockImplementation(async () => {
      throw new ApiError(404, 'That message is not in this chat.', 'That message is not in this chat.', null)
    })
    renderChat([reply('db-7', 'An answer.')])
    const dialog = await openAndSend('Private information')
    expect(within(dialog).getByRole('alert')).toHaveTextContent('That message is not in this chat.')
    expect(within(dialog).getByRole('button', { name: 'Send report' })).toBeEnabled()
  })
})

describe('the words the proxy takes', () => {
  // Mirrored by hand in the proxy's api/agents/reply_reports.py (its test pins the same).
  it('pins the four reasons and the note cap', () => {
    expect(REPLY_REPORT_REASONS.map((r) => r.value)).toEqual(['harmful', 'misleading', 'private', 'other'])
    expect(REPLY_REPORT_NOTE_MAX).toBe(1000)
  })
})

describe('naming the reply', () => {
  it('reads the first row of a stored bubble', () => {
    expect(storedRowId('db-3')).toBe(3)
    expect(storedRowId('db-evt-4')).toBe(4)
    expect(storedRowId('db-delresult-5')).toBe(5)
    expect(storedRowId('stream-6')).toBeNull()
    expect(storedRowId('live-turn-x')).toBeNull()
  })

  it('takes the newest equal reply and leaves progress notes out', () => {
    const streamed: DisplayMessage = {
      id: 'stream-1', role: 'assistant', createdAt: 't',
      blocks: [{ type: 'text', content: 'Looking it up.', note: true }, { type: 'text', content: 'Done.' }],
    }
    const rows = [
      row(1, 'assistant', 'Done.'), row(2, 'user', 'again'),
      row(3, 'assistant', 'Looking it up.', { event_data: '{"note": true}' }), row(4, 'assistant', 'Done.'),
    ]
    expect(findStoredReply([streamed], 0, rows)).toBe(3)
  })
})
