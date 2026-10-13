/**
 * The engine's own permission request refused with no person's answer
 * (`system` row, subtype `engine_prompt_refused`): the line names the call
 * and why, and says the turn went on.
 */
import { describe, it, expect } from 'vitest'
import { render, screen } from '@testing-library/react'
import SystemEvent from '@/components/chat/SystemEvent'
import { SYSTEM_SUBTYPE } from '@/api/wireEvents'

describe('the refused engine prompt line', () => {
  it('names the call and why it was refused', () => {
    render(<SystemEvent subtype={SYSTEM_SUBTYPE.ENGINE_PROMPT_REFUSED}
                        message={'The engine asked to run the command `rm -rf build`; nobody answered in time, so it was refused and the agent went on.'} />)
    expect(screen.getByTestId('engine-prompt-refused')).toBeTruthy()
    expect(screen.getByText('A permission request was refused')).toBeTruthy()
    expect(screen.getByText(/rm -rf build/)).toBeTruthy()
  })
})
