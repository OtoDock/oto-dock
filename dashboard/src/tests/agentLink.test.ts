import { describe, it, expect } from 'vitest'

import { agentLinkHere } from '@/lib/chat/agentLink'

const at = (pathname: string, search = '') => ({ pathname, search })

describe('agentLinkHere', () => {
  it("opens an agent's page link over the chat of that agent on screen", () => {
    expect(agentLinkHere('/chat/dev?recover=1', at('/chat/dev/c1'))).toBe('/chat/dev/c1?recover=1')
  })

  it("keeps the chat's workspace params and drops its others", () => {
    expect(agentLinkHere('/chat/dev?recover=1', at('/chat/dev/c1', '?ws=1&ws_preview=a.md&message=7&tasks=1')))
      .toBe('/chat/dev/c1?ws=1&ws_preview=a.md&recover=1')
  })

  it('a task chat of that agent counts as a chat', () => {
    expect(agentLinkHere('/chat/dev?recover=1', at('/chat/dev/task-r1')))
      .toBe('/chat/dev/task-r1?recover=1')
  })

  it("leaves the link alone from another agent's chat or page", () => {
    expect(agentLinkHere('/chat/dev?recover=1', at('/chat/ceo/c1'))).toBe('/chat/dev?recover=1')
    expect(agentLinkHere('/chat/dev?recover=1', at('/chat/dev-two/c1'))).toBe('/chat/dev?recover=1')
    expect(agentLinkHere('/chat/dev?recover=1', at('/agents/dev/config'))).toBe('/chat/dev?recover=1')
  })

  it("leaves the link alone on the agent's new-chat view", () => {
    expect(agentLinkHere('/chat/dev?recover=1', at('/chat/dev'))).toBe('/chat/dev?recover=1')
  })

  it('leaves a link to a chat, an app or a settings page alone', () => {
    expect(agentLinkHere('/chat/dev/c2', at('/chat/dev/c1'))).toBe('/chat/dev/c2')
    expect(agentLinkHere('/chat/dev/task-r2?tasks=1', at('/chat/dev/c1'))).toBe('/chat/dev/task-r2?tasks=1')
    expect(agentLinkHere('/apps/board', at('/chat/dev/c1'))).toBe('/apps/board')
    expect(agentLinkHere('/user-settings/ai-engines', at('/chat/dev/c1'))).toBe('/user-settings/ai-engines')
  })

  it("an agent's page link with no params stays on the chat", () => {
    expect(agentLinkHere('/chat/dev', at('/chat/dev/c1', '?ws=1'))).toBe('/chat/dev/c1?ws=1')
  })
})
