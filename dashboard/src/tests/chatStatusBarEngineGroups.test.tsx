import { describe, it, expect, vi } from 'vitest'
import { render, screen, fireEvent } from '@testing-library/react'

import ChatStatusBar from '@/components/chat/ChatStatusBar'

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
      mode="default"
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
    expect(selected.className).toContain('text-brand')
    expect(other.className).not.toContain('text-brand')
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
