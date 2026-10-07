import { describe, it, expect } from 'vitest'
import { render, screen } from '@testing-library/react'
import OpenAppToast from '../components/apps/OpenAppToast'

const base = { type: 'open_app' as const, app_id: 'a1', title: 'Register', scope_chat_id: '', scope_project_id: '' }

describe('OpenAppToast', () => {
  it('names the agent that asked, not the home agent of a placed app', () => {
    render(<OpenAppToast items={[{ ...base, agent: 'shared-test-agent', opened_by: 'personal-assistant-lite' }]}
      onOpen={() => {}} onDismiss={() => {}} />)
    expect(screen.getByTestId('open-app-toast').textContent)
      .toContain('The agent personal-assistant-lite wants to show you this app.')
  })
  it("names the app's own agent when that agent asked, and on an older proxy", () => {
    render(<OpenAppToast items={[{ ...base, agent: 'ops' }]} onOpen={() => {}} onDismiss={() => {}} />)
    expect(screen.getByTestId('open-app-toast').textContent).toContain('The agent ops wants to show you this app.')
  })
})
