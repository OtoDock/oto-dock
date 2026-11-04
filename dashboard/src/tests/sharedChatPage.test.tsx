import { describe, it, expect, vi } from 'vitest'
import { act, fireEvent, render, screen, within } from '@testing-library/react'
import { MemoryRouter, Route, Routes } from 'react-router-dom'

const snap = vi.hoisted(() => ({ current: null as unknown, loading: false }))
vi.mock('@/api/shares', () => ({
  useChatSnapshot: () => ({ data: snap.current, isLoading: snap.loading, error: null }),
}))
const reports = vi.hoisted(() => ({
  sent: vi.fn(async (_shareId: string, body: { message_id: number }) => body.message_id),
}))
vi.mock('@/api/replyReports', async (importOriginal) => ({
  ...(await importOriginal<typeof import('@/api/replyReports')>()),
  reportSharedReply: reports.sent,
}))
vi.mock('@/components/chat/MarkdownContent', () => ({
  default: ({ children }: { children: string }) => <div data-testid="md">{children}</div>,
}))

import SharedChatPage from '@/pages/SharedChatPage'

function renderPage() {
  return render(
    <MemoryRouter initialEntries={['/shared/s1']}>
      <Routes><Route path="/shared/:shareId" element={<SharedChatPage />} /></Routes>
    </MemoryRouter>,
  )
}

describe('SharedChatPage', () => {
  it('renders the snapshot read-only: text, a sandboxed artifact, media and files from the snapshot', () => {
    snap.current = {
      id: 's1', title: 'Plan review', agent: 'dev', created_at: new Date().toISOString(),
      shared_by_name: 'Nora', include_tools: false,
      messages: [
        { role: 'user', content: 'Show me the plan', created_at: '' },
        { role: 'assistant', content: 'Here **it** is', created_at: '' },
        { role: 'event', event_type: 'ui', created_at: '', data: { token: 'tok-ui', title: 'Chart', height: 300 } },
        { role: 'event', event_type: 'images', created_at: '', data: { images: [{ token: 'img-1', caption: 'a' }, { url: 'https://x/y.jpg' }] } },
        { role: 'event', event_type: 'file', created_at: '', data: { token: 'tok-f', filename: 'report.pdf' } },
      ],
    }
    const { container } = renderPage()
    expect(screen.getByRole('heading', { name: 'Plan review' })).toBeTruthy()
    expect(screen.getByText(/Shared by Nora/)).toBeTruthy()
    expect(screen.getByText('Show me the plan')).toBeTruthy()
    expect(screen.getByTestId('md').textContent).toBe('Here **it** is')
    const frame = container.querySelector('iframe')!
    expect(frame.getAttribute('src')).toBe('/v1/shares/s1/ui/tok-ui')
    expect(frame.getAttribute('sandbox')).toBe('allow-scripts')
    const imgs = Array.from(container.querySelectorAll('img')).map((i) => i.getAttribute('src'))
    expect(imgs).toEqual(['/v1/shares/s1/media/img-1', 'https://x/y.jpg'])
    expect(screen.getByText('report.pdf').closest('a')!.getAttribute('href')).toBe('/v1/shares/s1/media/tok-f')
  })

  it("draws a settings card as a placeholder naming the setting, never a value", () => {
    // The proxy's copy of a card row (services/sharing/chat_snapshot._card_block):
    // an artifact-shaped block whose data holds the id and the label alone.
    snap.current = {
      id: 's1', title: 'Settings', agent: 'dev', created_at: new Date().toISOString(),
      shared_by_name: 'Nora', include_tools: false,
      messages: [
        { role: 'assistant', content: 'Here is the card.', created_at: '' },
        { role: 'event', event_type: 'settings_card', created_at: '', data: { setting: 'user.general.wake-word', label: 'Wake word' } },
        { role: 'event', event_type: 'settings_card', created_at: '', data: { setting: 'user.general.gone', label: '' } },
      ],
    }
    renderPage()
    expect(screen.getAllByTestId('snapshot-setting').map((n) => n.textContent)).toEqual([
      'A settings card for Wake word. It shows in the chat only.',
      'A settings card for a setting. It shows in the chat only.',
    ])
    expect(screen.queryByRole('switch')).toBeNull()
  })

  it('draws a list card as a placeholder naming the list, never a row', () => {
    snap.current = {
      id: 's1', title: 'Lists', agent: 'dev', created_at: new Date().toISOString(),
      shared_by_name: 'Nora', include_tools: false,
      messages: [
        { role: 'assistant', content: 'Here they are.', created_at: '' },
        { role: 'event', event_type: 'list_card', created_at: '', data: { kind: 'tasks', title: 'Your scheduled tasks', person: 'u1' } },
        { role: 'event', event_type: 'list_card', created_at: '', data: { kind: 'machines', title: '' } },
      ],
    }
    renderPage()
    expect(screen.getAllByTestId('snapshot-list').map((n) => n.textContent)).toEqual([
      'A list card: Your scheduled tasks. It shows in the chat only.',
      'A list card: a list. It shows in the chat only.',
    ])
    expect(screen.queryByRole('link', { name: 'Open the page' })).toBeNull()
    expect(screen.queryByRole('list')).toBeNull()
  })

  it("draws a person's message of photos alone with their count, never the photos", () => {
    snap.current = {
      id: 's1', title: 'Photos', agent: 'dev', created_at: new Date().toISOString(),
      shared_by_name: 'Nora', include_tools: false,
      messages: [
        { role: 'user', content: '', created_at: '', attachments: { photos: 2, files: 0 } },
        { role: 'user', content: 'and this', created_at: '', attachments: { photos: 1, files: 1 } },
      ],
    }
    const { container } = renderPage()
    const lines = screen.getAllByTestId('snapshot-attachments').map((n) => n.textContent)
    expect(lines).toEqual(['2 photos attached', '1 photo and 1 file attached'])
    expect(screen.getByText('and this')).toBeTruthy()
    expect(container.querySelectorAll('img')).toHaveLength(0)
  })

  it('says so when the share is gone', () => {
    snap.current = null
    renderPage()
    expect(screen.getByText('This shared chat is not available.')).toBeTruthy()
  })

  it('puts one flag under each run of the agent\'s blocks and reports it through the share', async () => {
    snap.current = {
      id: 's1', title: 'Plan review', agent: 'dev', created_at: new Date().toISOString(),
      shared_by_name: 'Nora', include_tools: false,
      messages: [
        { role: 'user', content: 'Show me the plan', created_at: '' },
        { id: 11, role: 'assistant', content: 'Here it is', created_at: '' },
        { id: 12, role: 'event', event_type: 'ui', created_at: '', data: { token: 'tok-ui', title: 'Chart' } },
        { role: 'user', content: 'Thanks', created_at: '' },
        { id: 21, role: 'assistant', content: 'You are welcome', created_at: '' },
      ],
    }
    renderPage()
    const flags = screen.getAllByRole('button', { name: 'Report this reply' })
    expect(flags).toHaveLength(2)
    fireEvent.click(flags[0])
    const dialog = await screen.findByTestId('report-reply-dialog')
    fireEvent.click(within(dialog).getByRole('radio', { name: 'Harmful or offensive' }))
    await act(async () => { fireEvent.click(within(dialog).getByRole('button', { name: 'Send report' })) })
    expect(reports.sent).toHaveBeenCalledWith('s1', { message_id: 11, reason: 'harmful', note: '' })
    fireEvent.click(within(dialog).getByRole('button', { name: 'Done' }))
    expect(screen.getAllByRole('button', { name: 'Reported' })).toHaveLength(1)
  })

  it('shows no flag on a copy made before the rows were kept', () => {
    snap.current = {
      id: 's1', title: 'Old copy', agent: 'dev', created_at: new Date().toISOString(),
      shared_by_name: 'Nora', include_tools: false,
      messages: [
        { role: 'user', content: 'Q', created_at: '' },
        { role: 'assistant', content: 'A', created_at: '' },
      ],
    }
    renderPage()
    expect(screen.queryByRole('button', { name: 'Report this reply' })).toBeNull()
  })

  it('keeps one flag per reply when a thinking block sits inside it', async () => {
    snap.current = {
      id: 's1', title: 'With tools', agent: 'dev', created_at: new Date().toISOString(),
      shared_by_name: 'Nora', include_tools: true,
      messages: [
        { role: 'user', content: 'Plan it', created_at: '' },
        { id: 11, role: 'assistant', content: 'First part', created_at: '' },
        { role: 'event', event_type: 'thinking', created_at: '', event_data: '{"text":"x"}' },
        { id: 13, role: 'assistant', content: 'Second part', created_at: '' },
        { role: 'user', content: 'Thanks', created_at: '' },
        { role: 'event', event_type: 'thinking', created_at: '', event_data: '{"text":"y"}' },
        { id: 21, role: 'assistant', content: 'You are welcome', created_at: '' },
      ],
    }
    renderPage()
    const flags = screen.getAllByRole('button', { name: 'Report this reply' })
    expect(flags).toHaveLength(2)
    fireEvent.click(flags[1])
    const dialog = await screen.findByTestId('report-reply-dialog')
    fireEvent.click(within(dialog).getByRole('radio', { name: 'Something else' }))
    await act(async () => { fireEvent.click(within(dialog).getByRole('button', { name: 'Send report' })) })
    expect(reports.sent).toHaveBeenLastCalledWith('s1', { message_id: 21, reason: 'other', note: '' })
  })
})
