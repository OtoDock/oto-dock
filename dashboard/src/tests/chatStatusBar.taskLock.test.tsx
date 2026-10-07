import { describe, it, expect, vi } from 'vitest'
import { render, screen, fireEvent } from '@testing-library/react'

import ChatStatusBar from '@/components/chat/ChatStatusBar'

// ─── Lock behaviors of the status-bar selectors.
// modeLocked: task-run chats keep the RUN's permission posture read-only.
// modelLocked: INTERACTIVE PTY sessions only since 1.5 — task chats now flow
// through computeModelGroups like every chat (see modelGroups.test.ts for
// the alive→same-engine / dead→cross-engine rule and the retired-model
// prepend); the locked rendering pinned here still covers the PTY lock.

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
      costUsd={0}
      contextUsed={0}
      contextMax={0}
      onModeChange={() => {}}
      onModelChange={() => {}}
      {...over}
    />,
  )
}

describe('ChatStatusBar task-run locks', () => {
  it('modeLocked shows the run mode read-only (single option, no-op select)', () => {
    const onModeChange = vi.fn()
    renderBar({ mode: 'dontAsk', modeLocked: true, onModeChange })

    fireEvent.click(screen.getByTitle("Mode: Don't Ask"))
    const options = screen.getAllByRole('button').filter(b => b.textContent === "Don't Ask")
    expect(options).toHaveLength(1)
    expect(screen.queryByText('Accept Edits')).toBeNull()

    fireEvent.click(options[0])
    expect(onModeChange).not.toHaveBeenCalled()
  })

  it('modelLocked renders an unlisted model id as the selected row', () => {
    const onModelChange = vi.fn()
    renderBar({
      model: 'claude-opus-4-6',  // not in the served catalog anymore
      modelLocked: true,
      onModelChange,
      modelOptions: [
        { value: 'claude-fable-5', label: 'Fable 5' },
        { value: 'claude-sonnet-5', label: 'Sonnet 5' },
      ],
    })

    fireEvent.click(screen.getByTitle('Model: claude-opus-4-6'))
    // The locked popup lists ONLY the active model (raw id fallback label).
    expect(screen.getByText('claude-opus-4-6')).toBeTruthy()
    expect(screen.queryByText('Fable 5')).toBeNull()

    fireEvent.click(screen.getByText('claude-opus-4-6'))
    expect(onModelChange).not.toHaveBeenCalled()
  })

  it('unlocked mode dropdown still offers the full option set', () => {
    renderBar({ mode: 'default' })
    fireEvent.click(screen.getByTitle('Mode: Default'))
    expect(screen.getByText('Accept Edits')).toBeTruthy()
    expect(screen.getByText("Don't Ask")).toBeTruthy()
  })
})

describe('ChatStatusBar on a chat that runs as the agent, below the editor tier', () => {
  const reason = 'This agent is set to Shared only, so its chats and tasks run as the agent itself, which takes the editor role or above (this one would run as contributor).'

  it('locks the mode, model and terminal pickers and names why', () => {
    const onModeChange = vi.fn()
    const onModelChange = vi.fn()
    const onInteractiveToggle = vi.fn()
    renderBar({
      mode: 'default', modeLocked: true, modelLocked: true,
      interactiveAvailable: true, interactiveOn: false, interactiveDisabled: true,
      lockReason: reason, onModeChange, onModelChange, onInteractiveToggle,
      modelOptions: [
        { value: 'claude-fable-5', label: 'Fable 5' },
        { value: 'claude-sonnet-5', label: 'Sonnet 5' },
      ],
    })
    // The triggers keep their "Mode:" / "Model:" prefix and carry the reason.
    const modeTrigger = screen.getByTitle(`Mode: Default · ${reason}`)
    fireEvent.click(modeTrigger)
    expect(screen.queryByText('Accept Edits')).toBeNull()

    fireEvent.click(screen.getByTitle(`Model: Sonnet 5 · ${reason}`))
    expect(screen.queryByText('Fable 5')).toBeNull()
    const toggle = screen.getByRole('switch')
    expect(toggle.getAttribute('title')).toBe(reason)
    fireEvent.click(toggle)
    expect(onInteractiveToggle).not.toHaveBeenCalled()
    expect(onModeChange).not.toHaveBeenCalled()
    expect(onModelChange).not.toHaveBeenCalled()
  })
})
