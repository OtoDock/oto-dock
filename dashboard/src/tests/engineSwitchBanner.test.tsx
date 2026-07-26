import { describe, it, expect, vi } from 'vitest'
import { render, screen, fireEvent } from '@testing-library/react'

import EngineSwitchBanner, { EngineSwitchConfirm } from '@/components/chat/EngineSwitchBanner'

// ─── Cross-engine switch banner + confirm dialog ────────────────────────────
// A provisional cross-engine model pick on a dead chat: banner (button + ✕)
// → confirm dialog (esc-stack portal) → switch. Denials render INSIDE the
// dialog (the generic WS error rail is invisible on idle dead chats).

const PENDING = {
  layer: 'codex-cli',
  layerLabel: 'OpenAI Codex',
  model: 'gpt-5',
  modelLabel: 'GPT-5',
}

function renderBanner(over: Partial<Parameters<typeof EngineSwitchBanner>[0]> = {}) {
  return render(
    <EngineSwitchBanner
      chatId="c1"
      pending={PENDING}
      fromLabel="Claude Code CLI"
      busy={false}
      error={null}
      onConfirm={() => {}}
      onCancel={() => {}}
      {...over}
    />,
  )
}

describe('EngineSwitchBanner', () => {
  it('renders nothing without a pending pick or a chat', () => {
    expect(renderBanner({ pending: null }).container.firstChild).toBeNull()
    expect(renderBanner({ chatId: null }).container.firstChild).toBeNull()
  })

  it('states the restart-from-history consequence with both engine labels', () => {
    renderBanner()
    const banner = screen.getByTestId('engine-switch-banner')
    expect(banner.textContent).toContain('OpenAI Codex')
    expect(banner.textContent).toContain('GPT-5')
    expect(banner.textContent).toContain('restarted from database history')
    expect(banner.textContent).toContain('Claude Code CLI session file')
  })

  it('✕ cancels (reverts the provisional selection)', () => {
    const onCancel = vi.fn()
    renderBanner({ onCancel })
    fireEvent.click(screen.getByTitle('Keep the current engine'))
    expect(onCancel).toHaveBeenCalledTimes(1)
  })

  it('the switch button opens the confirm; Cancel closes without confirming', () => {
    const onConfirm = vi.fn()
    renderBanner({ onConfirm })
    fireEvent.click(screen.getByText('Switch to OpenAI Codex'))
    expect(screen.getByText('Switch this chat to OpenAI Codex?')).toBeTruthy()
    fireEvent.click(screen.getByText('Cancel'))
    expect(screen.queryByText('Switch this chat to OpenAI Codex?')).toBeNull()
    expect(onConfirm).not.toHaveBeenCalled()
  })

  it('confirming fires onConfirm', () => {
    const onConfirm = vi.fn()
    renderBanner({ onConfirm })
    fireEvent.click(screen.getByText('Switch to OpenAI Codex'))
    fireEvent.click(screen.getByText('Switch engine'))
    expect(onConfirm).toHaveBeenCalledTimes(1)
  })

  it('denial text renders inside the open dialog', () => {
    renderBanner({ error: null })
    fireEvent.click(screen.getByText('Switch to OpenAI Codex'))
    // The page flips `error` after switch_engine_denied — re-render with it.
    renderBanner({ error: 'The session is still active — switching engines is possible once it has ended.' })
    expect(screen.getAllByTestId('engine-switch-banner').length).toBeGreaterThan(0)
  })
})

describe('EngineSwitchConfirm', () => {
  const base = {
    fromLabel: 'Claude Code CLI',
    toLabel: 'OpenAI Codex',
    busy: false,
    error: null as string | null,
    onConfirm: () => {},
    onCancel: () => {},
  }

  it('states the honest fork-from-history consequence', () => {
    render(<EngineSwitchConfirm {...base} />)
    const body = document.body.textContent || ''
    expect(body).toContain('loads this chat\'s history')
    expect(body).toContain('Claude Code CLI session file is left')
  })

  it('busy disables both buttons and shows progress', () => {
    const onConfirm = vi.fn()
    render(<EngineSwitchConfirm {...base} busy onConfirm={onConfirm} />)
    const btn = screen.getByText('Switching…')
    expect((btn as HTMLButtonElement).disabled).toBe(true)
    fireEvent.click(btn)
    expect(onConfirm).not.toHaveBeenCalled()
  })

  it('renders the switch_engine_denied message', () => {
    render(<EngineSwitchConfirm {...base} error="The chat changed underneath — try again." />)
    expect(screen.getByTestId('engine-switch-error').textContent)
      .toBe('The chat changed underneath — try again.')
  })

  it('backdrop click cancels (unless busy)', () => {
    const onCancel = vi.fn()
    const { rerender } = render(<EngineSwitchConfirm {...base} onCancel={onCancel} />)
    fireEvent.click(document.querySelector('.fixed.inset-0')!)
    expect(onCancel).toHaveBeenCalledTimes(1)
    rerender(<EngineSwitchConfirm {...base} busy onCancel={onCancel} />)
    fireEvent.click(document.querySelector('.fixed.inset-0')!)
    expect(onCancel).toHaveBeenCalledTimes(1)  // busy swallows it
  })
})
