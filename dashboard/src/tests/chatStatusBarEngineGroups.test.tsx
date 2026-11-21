import { afterEach, describe, it, expect, vi } from 'vitest'
import { render, screen, fireEvent } from '@testing-library/react'

import ChatStatusBar from '@/components/chat/ChatStatusBar'
import { modeBusyReason } from '@/lib/engines/modes'

// ─── Model dropdown: multi-engine groups + the display-only overrides ───────
// On a dead chat the page passes expanded groups (chat engine first) and,
// while a cross-engine pick is pending, display-only `model`/`modelValue`
// overrides — the checkmark must follow `modelValue`, and opening the menu
// must fire the lazy liveness probe.

const GROUPS = [
  {
    layer: 'claude-code-cli', layerLabel: 'Claude Code CLI',
    models: [{ value: 'claude-code-cli::claude-sonnet-5', label: 'Sonnet 5' }],
  },
  {
    layer: 'codex-cli', layerLabel: 'OpenAI Codex',
    models: [{ value: 'codex-cli::gpt-5', label: 'GPT-5' }],
  },
]

function renderBar(over: Partial<Parameters<typeof ChatStatusBar>[0]> = {}) {
  return render(
    <ChatStatusBar
      streaming={false}
      warming={false}
      startTime={null}
      thinkingActive={false}
      compressingActive={false}
      activeAgents={[]}
      mode="ask"
      model="claude-sonnet-5"
      modelValue="claude-code-cli::claude-sonnet-5"
      modelGroups={GROUPS}
      costUsd={0}
      contextUsed={0}
      contextMax={0}
      onModeChange={() => {}}
      onModelChange={() => {}}
      {...over}
    />,
  )
}

const openModelMenu = () =>
  fireEvent.click(screen.getByTitle(/^Model:/))

describe('ChatStatusBar — expanded engine groups', () => {
  it('renders every group with its layer header', () => {
    renderBar()
    openModelMenu()
    expect(screen.getByText('Claude Code CLI')).toBeTruthy()
    expect(screen.getByText('OpenAI Codex')).toBeTruthy()
    expect(screen.getByText('Sonnet 5')).toBeTruthy()
    expect(screen.getByText('GPT-5')).toBeTruthy()
  })

  it('the checkmark follows modelValue — incl. the pending cross-engine override', () => {
    renderBar({ model: 'gpt-5', modelValue: 'codex-cli::gpt-5' })
    openModelMenu()
    const selected = screen.getByText('GPT-5').closest('button')!
    const other = screen.getByText('Sonnet 5').closest('button')!
    expect(selected.className).toContain('text-link')
    expect(other.className).not.toContain('text-link')
  })

  it('selecting an option reports the compound value', () => {
    const onModelChange = vi.fn()
    renderBar({ onModelChange })
    openModelMenu()
    fireEvent.click(screen.getByText('GPT-5'))
    expect(onModelChange).toHaveBeenCalledWith('codex-cli::gpt-5')
  })

  it('opening the model menu fires the lazy liveness probe', () => {
    const onModelMenuOpen = vi.fn()
    renderBar({ onModelMenuOpen })
    openModelMenu()
    expect(onModelMenuOpen).toHaveBeenCalledTimes(1)
    // Closing does not re-fire.
    openModelMenu()
    expect(onModelMenuOpen).toHaveBeenCalledTimes(1)
  })
})

// ─── The right-hand controls' order ─────────────────────────────────────────
// Headless: the "Runs on" pill, the mode icon, the model button. On a live
// interactive terminal (no mode icon): the pill, the view toggle, the model
// button. The bar has no width-dependent order, so 390 px reads as 1280 px.

const PILL = <span data-testid="runs-on-chip" className="w-7 h-7 rounded-lg border" title="Runs on OtoDock platform" />

function rightControls(): string[] {
  const named: [string, Element | null][] = [
    ['pill', screen.queryByTestId('runs-on-chip')],
    ['view', screen.queryByTitle(/^(Show the conversation history|Back to the live terminal)$/)],
    ['mode', screen.queryByTitle(/^Mode:/)],
    ['model', screen.queryByTitle(/^Model:/)],
  ]
  return named
    .filter((e): e is [string, Element] => e[1] !== null)
    .sort(([, a], [, b]) => (a.compareDocumentPosition(b) & Node.DOCUMENT_POSITION_FOLLOWING ? -1 : 1))
    .map(([n]) => n)
}

describe('ChatStatusBar — the right-hand controls', () => {
  const width = window.innerWidth
  afterEach(() => {
    Object.defineProperty(window, 'innerWidth', { value: width, configurable: true })
  })

  it.each([1280, 390])('headless at %i px: the pill, the mode icon, the model', (px) => {
    Object.defineProperty(window, 'innerWidth', { value: px, configurable: true })
    renderBar({ targetSlot: PILL })
    expect(rightControls()).toEqual(['pill', 'mode', 'model'])
  })

  it.each([1280, 390])('on a live terminal at %i px: the view toggle, the pill, the mode, the model', (px) => {
    Object.defineProperty(window, 'innerWidth', { value: px, configurable: true })
    renderBar({ targetSlot: PILL, richViewAvailable: true, interactiveActive: true })
    expect(rightControls()).toEqual(['view', 'pill', 'mode', 'model'])
  })

  it('the terminal order holds while the history view is shown', () => {
    renderBar({ targetSlot: PILL, richViewAvailable: true, richViewActive: true, interactiveActive: true })
    expect(rightControls()).toEqual(['view', 'pill', 'mode', 'model'])
  })

  it('a live terminal with the toggle on and no terminal yet: no view toggle, the mode shown', () => {
    renderBar({ targetSlot: PILL, interactiveActive: true })
    expect(rightControls()).toEqual(['pill', 'mode', 'model'])
  })

  it('a turn on a live terminal and a restart make the mode button busy; a headless turn never', () => {
    const idle = { terminalLive: false, terminalChat: false, switching: false, starting: false, turnRuns: false }
    const live = { ...idle, terminalLive: true, terminalChat: true }
    expect(modeBusyReason({ ...live, turnRuns: true })).toBe('Finish or stop the current turn before changing the mode.')
    expect(modeBusyReason(live)).toBe('')
    expect(modeBusyReason({ ...idle, turnRuns: true })).toBe('')
    expect(modeBusyReason({ ...idle, switching: true })).toBe('The chat is starting again.')
    // A terminal chat whose terminal is starting: its start has read the mode.
    expect(modeBusyReason({ ...idle, terminalChat: true, starting: true }))
      .toBe('The terminal is starting. Pick the mode again when it is up.')
    // A terminal chat with nothing starting: the pick goes to the row.
    expect(modeBusyReason({ ...idle, terminalChat: true })).toBe('')
    // A headless chat starting: its pick reaches the start.
    expect(modeBusyReason({ ...idle, starting: true })).toBe('')
  })

  it('a busy mode button takes no pick, keeps its place and says why on a click', () => {
    const onModeChange = vi.fn()
    const reason = 'Finish or stop the current turn before changing the mode.'
    renderBar({
      targetSlot: PILL, richViewAvailable: true, interactiveActive: true, mode: 'dontAsk',
      modeBusyReason: reason, onModeChange,
    })
    expect(rightControls()).toEqual(['view', 'pill', 'mode', 'model'])
    const button = screen.getByTestId('mode-busy')
    expect(button.getAttribute('aria-disabled')).toBe('true')
    expect(button.getAttribute('title')).toBe(`Mode: Don't Ask. ${reason}`)
    fireEvent.click(button)
    expect(screen.getByTestId('runs-on-note').textContent).toContain(reason)
    expect(screen.queryByRole('menu')).toBeNull()
    expect(onModeChange).not.toHaveBeenCalled()
  })
})
