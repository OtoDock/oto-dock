import { describe, it, expect, vi } from 'vitest'
import { render, screen, fireEvent } from '@testing-library/react'

import ChatStatusBar from '@/components/chat/ChatStatusBar'

// ─── Task-run chats: the model/permission selectors show the RUN's facts
// (agent default model, 'auto' → Don't Ask) read-only — never the viewer's
// sticky selections, and the model renders even when the layer's current
// catalog no longer lists it (an agent configured with a retired model id).

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
