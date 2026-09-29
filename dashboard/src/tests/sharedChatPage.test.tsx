import { describe, it, expect, vi } from 'vitest'
import { render, screen } from '@testing-library/react'
import { MemoryRouter, Route, Routes } from 'react-router-dom'

const snap = vi.hoisted(() => ({ current: null as unknown, loading: false }))
vi.mock('@/api/shares', () => ({
  useChatSnapshot: () => ({ data: snap.current, isLoading: snap.loading, error: null }),
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

  it('says so when the share is gone', () => {
    snap.current = null
    renderPage()
    expect(screen.getByText('This shared chat is not available.')).toBeTruthy()
  })
})
