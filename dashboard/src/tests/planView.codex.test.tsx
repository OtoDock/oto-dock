/**
 * PlanView renders the CODEX implement affordance from a synthesized exit card.
 *
 * A codex plan card has plan CONTENT but no plan FILE (isCodexPlan), so the
 * implement buttons must call onImplementCodex (mode switch + build turn), not
 * onImplement (implement_plan {plan_path}). Regression guard for the assistant
 * render path, which had dropped onImplementCodex — the card showed only
 * Edit/Reject, never "Start Implementation".
 */
import { describe, it, expect, vi } from 'vitest'
import { render, screen, fireEvent } from '@testing-library/react'
import PlanView from '@/components/chat/plan/PlanView'

describe('PlanView — codex synthesized plan', () => {
  it('shows codex implement buttons and calls onImplementCodex', () => {
    const onImplementCodex = vi.fn()
    const onImplement = vi.fn()
    render(
      <PlanView
        action="exit"
        toolInput={{ plan: '- Add --version\n- Add a CLI test' }}
        onImplement={onImplement}
        onImplementCodex={onImplementCodex}
        onSendMessage={vi.fn()}
      />,
    )
    fireEvent.click(screen.getByRole('button', { name: 'Start Implementation' }))
    expect(onImplementCodex).toHaveBeenCalledWith('acceptEdits')
    fireEvent.click(screen.getByRole('button', { name: 'Full Permissions' }))
    expect(onImplementCodex).toHaveBeenCalledWith('dontAsk')
    // Never the Claude file-based path for a codex (fileless) plan.
    expect(onImplement).not.toHaveBeenCalled()
  })

  it('a superseded codex card is inert (content only, no action buttons)', () => {
    render(
      <PlanView
        action="exit"
        superseded
        toolInput={{ plan: 'old plan' }}
        onImplementCodex={vi.fn()}
        onSendMessage={vi.fn()}
      />,
    )
    expect(screen.queryByRole('button', { name: 'Start Implementation' })).toBeNull()
    expect(screen.queryByRole('button', { name: 'Reject' })).toBeNull()
  })
})
