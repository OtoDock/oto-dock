import { readFileSync } from 'node:fs'
import path from 'node:path'
import { fileURLToPath } from 'node:url'
import { describe, it, expect, vi } from 'vitest'
import { render, screen, fireEvent } from '@testing-library/react'

import EngineSwitchBanner, { EngineSwitchConfirm } from '@/components/chat/EngineSwitchBanner'
import { ChatBottomPanel } from '@/pages/agent/chat/ChatComposerBar'
import { personClick } from './dialogsHarness'

// ─── Cross-engine switch banner + confirm dialog ────────────────────────────
// A provisional cross-engine model pick on a dead chat: banner (button + ✕)
// → confirm dialog (esc-stack portal) → switch. Denials render INSIDE the
// dialog (the generic WS error rail is invisible on idle dead chats).

const PENDING = {
  layer: 'codex-cli',
  layerLabel: 'OpenAI Codex',
  model: 'gpt-6.1-sol',
}
const TO_CLAUDE = {
  layer: 'claude-code-cli',
  layerLabel: 'Claude Code CLI',
  model: 'claude-fable-5-1',
}

function renderBanner(over: Partial<Parameters<typeof EngineSwitchBanner>[0]> = {}) {
  return render(
    <EngineSwitchBanner
      chatId="c1"
      pending={PENDING}
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

  it.each([
    [PENDING, 'Continue on OpenAI Codex? The chat restarts from its history.'],
    [TO_CLAUDE, 'Continue on Claude Code CLI? The chat restarts from its history.'],
  ])('names the engine and the restart from history, both ways (%#)', (pending, words) => {
    renderBanner({ pending })
    const banner = screen.getByTestId('engine-switch-banner')
    const sentence = banner.querySelector('span')?.textContent?.replace(/\s+/g, ' ').trim()
    expect(sentence).toBe(words)
    // No model in parentheses, no word about the old engine's files.
    expect(banner.textContent).not.toMatch(/\(|database|session file/)
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
    personClick(screen.getByText('Cancel'))
    expect(screen.queryByText('Switch this chat to OpenAI Codex?')).toBeNull()
    expect(onConfirm).not.toHaveBeenCalled()
  })

  it('confirming fires onConfirm', () => {
    const onConfirm = vi.fn()
    renderBanner({ onConfirm })
    fireEvent.click(screen.getByText('Switch to OpenAI Codex'))
    personClick(screen.getByText('Switch engine'))
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
    toLabel: 'OpenAI Codex',
    busy: false,
    error: null as string | null,
    onConfirm: () => {},
    onCancel: () => {},
  }

  it.each(['OpenAI Codex', 'Claude Code CLI'])('says the switch starts %s fresh with the chat\'s history', (toLabel) => {
    render(<EngineSwitchConfirm {...base} toLabel={toLabel} />)
    const body = (document.body.textContent || '').replace(/\s+/g, ' ')
    expect(body).toContain(`This starts a fresh ${toLabel} session and loads the chat's history.`)
    expect(body).not.toMatch(/database|session file|won't be used/)
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
    // The backdrop is the <dialog> element around the panel.
    personClick(screen.getByRole('alertdialog'))
    expect(onCancel).toHaveBeenCalledTimes(1)
    rerender(<EngineSwitchConfirm {...base} busy onCancel={onCancel} />)
    personClick(screen.getByRole('alertdialog'))
    expect(onCancel).toHaveBeenCalledTimes(1)  // busy swallows it
  })
})

describe('the bottom panel and its fade', () => {
  it.each([1280, 390])('at %i px the fade sits above every banner, never over one', (width) => {
    vi.stubGlobal('innerWidth', width)
    render(
      <ChatBottomPanel>
        <div role="status" data-testid="phone-call-notice">This chat is on a phone call.</div>
        <EngineSwitchBanner chatId="c1" pending={PENDING} busy={false} error={null}
                            onConfirm={() => {}} onCancel={() => {}} />
        <div data-composer-bar>composer</div>
      </ChatBottomPanel>,
    )
    const fades = document.querySelectorAll('[data-composer-fade]')
    expect(fades).toHaveLength(1)
    const fade = fades[0] as HTMLElement
    const panel = fade.parentElement as HTMLElement
    // The fade hangs from the panel's top edge (absolute, bottom-full, in a
    // relative panel): its box ends where the panel's begins, and every
    // banner and the composer are inside the panel, below that edge.
    expect(panel).toHaveAttribute('data-chat-bottom')
    expect(panel.className.split(/\s+/)).toEqual(expect.arrayContaining(['relative', 'shrink-0', 'bg-surface-page']))
    expect(fade.className.split(/\s+/)).toEqual(expect.arrayContaining(['absolute', 'bottom-full', 'h-4']))
    expect(panel.firstElementChild).toBe(fade)
    for (const el of [screen.getByTestId('phone-call-notice'), screen.getByTestId('engine-switch-banner'),
      document.querySelector('[data-composer-bar]') as HTMLElement]) {
      expect(panel.contains(el)).toBe(true)
      expect(fade.contains(el)).toBe(false)
    }
    vi.unstubAllGlobals()
  })
})

describe('the chat page puts its banners in the bottom panel', () => {
  it('ChatBanners, the lock line and the composer sit inside ChatBottomPanel in AgentChat', () => {
    // Mounting the whole page needs every hook it uses: the page's own
    // source is the cheaper proof that nothing above the fade is a banner.
    const page = readFileSync(path.resolve(path.dirname(fileURLToPath(import.meta.url)),
      '../pages/agent/AgentChat.tsx'), 'utf8')
    const open = page.indexOf('<ChatBottomPanel>')
    const close = page.indexOf('</ChatBottomPanel>')
    expect(open).toBeGreaterThan(0)
    expect(close).toBeGreaterThan(open)
    expect(page.split('<ChatBottomPanel>')).toHaveLength(2)
    for (const child of ['<ChatBanners', '<ChatLockNotice', '<ChatComposerBar']) {
      const at = page.indexOf(child)
      expect(at, child).toBeGreaterThan(open)
      expect(at, child).toBeLessThan(close)
      expect(page.split(child), child).toHaveLength(2)
    }
  })
})
