import { describe, it, expect } from 'vitest'

import { planOpenApp } from '@/lib/openApp'
import type { OpenAppFrame } from '@/lib/appLive'

const frame = (over: Partial<OpenAppFrame> = {}): OpenAppFrame => ({
  type: 'open_app', app_id: 'app-1', title: 'Board', agent: 'a1',
  scope_chat_id: '', scope_project_id: '', ...over,
})

describe('planOpenApp', () => {
  it('opens a standing app of this agent in the overlay', () => {
    expect(planOpenApp(frame(), { agentName: 'a1', chatId: 'c1', visible: true })).toBe('overlay')
    expect(planOpenApp(frame(), { agentName: 'a1', visible: true })).toBe('overlay')
  })

  it('leaves another agent, a hidden tab and a project pin to the toast', () => {
    expect(planOpenApp(frame(), { agentName: 'a2', chatId: 'c1', visible: true })).toBe('ignore')
    expect(planOpenApp(frame(), { agentName: undefined, visible: true })).toBe('ignore')
    expect(planOpenApp(frame(), { agentName: 'a1', chatId: 'c1', visible: false })).toBe('ignore')
    expect(planOpenApp(frame({ scope_project_id: 'p1' }), { agentName: 'a1', chatId: 'c1', visible: true })).toBe('ignore')
  })

  it('opens a chat pin on its Dock only when the viewer is in that chat', () => {
    expect(planOpenApp(frame({ scope_chat_id: 'c1' }), { agentName: 'a1', chatId: 'c1', visible: true })).toBe('dock')
    expect(planOpenApp(frame({ scope_chat_id: 'c1' }), { agentName: 'a1', chatId: 'c2', visible: true })).toBe('ignore')
    expect(planOpenApp(frame({ scope_chat_id: 'c1' }), { agentName: 'a1', visible: true })).toBe('ignore')
  })
})
