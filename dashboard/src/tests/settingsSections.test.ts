// The three settings areas' section lists, their gates, and the paths the
// shell, the guards and the redirect read.
import { describe, it, expect } from 'vitest'
import {
  adminSections, agentSections, userSections, currentSection, isUserSettingsPath,
  settingsBaseOf, userSettingsTabTarget,
} from '@/components/settings/sections'
import { USER_SETTINGS_TABS } from '@/lib/openTarget'

const shown = (list: { label: string; visible?: boolean }[]) =>
  list.filter((s) => s.visible !== false).map((s) => s.label)

describe('adminSections', () => {
  const list = adminSections({ remoteMachinesAvailable: true, pendingMcpRequests: 3 })

  it('puts Setup beside Overview and Monitoring last', () => {
    expect(list.map((s) => s.label)).toEqual([
      'Overview', 'Setup', 'Users', 'Usage', 'MCP Servers', 'Skills', 'MCP Requests',
      'Remote Machines', 'Shares', 'Reports',
      'Scheduled Tasks', 'Triggers', 'Notifications', 'Task History', 'Meetings',
    ])
    expect(list.filter((s) => s.group === 'Monitoring').map((s) => s.label))
      .toEqual(['Scheduled Tasks', 'Triggers', 'Notifications', 'Task History', 'Meetings'])
    expect(list.find((s) => s.label === 'Setup')?.path).toBe('/admin/platform')
  })

  it('names Overview as the index, carries the badge, and hides Remote Machines with the feature', () => {
    expect(list.filter((s) => s.index).map((s) => s.path)).toEqual(['/admin/overview'])
    expect(list.find((s) => s.label === 'MCP Requests')?.badge).toBe(3)
    expect(shown(adminSections({ remoteMachinesAvailable: false, pendingMcpRequests: 0 })))
      .not.toContain('Remote Machines')
  })

  it('gives every row an icon and a path under /admin', () => {
    for (const s of list) {
      expect(typeof s.icon).toBe('function')
      expect(s.path.startsWith('/admin/')).toBe(true)
    }
  })
})

describe('agentSections', () => {
  it('shows a manager every row', () => {
    expect(shown(agentSections('ops', true))).toEqual([
      'Overview', 'MCPs', 'Skills', 'Configuration', 'Checks',
      'Conversations', 'Scheduled Tasks', 'Triggers', 'Notifications', 'Meetings',
    ])
  })

  it('hides the manager rows from everyone else', () => {
    expect(shown(agentSections('ops', false))).toEqual([
      'Overview', 'Checks', 'Scheduled Tasks', 'Triggers', 'Notifications', 'Meetings',
    ])
  })

  it('builds the paths under the agent, Overview on its own route', () => {
    const list = agentSections('ops', true)
    expect(list[0].path).toBe('/agents/ops/overview')
    expect(list.find((s) => s.label === 'Configuration')?.path).toBe('/agents/ops/config')
    expect(list.find((s) => s.label === 'Conversations')?.group).toBe('Monitoring')
  })
})

describe('userSections', () => {
  it('turns the tabs into sections, General first', () => {
    const list = userSections({ remoteMachinesAvailable: true })
    expect(list.map((s) => s.path)).toEqual(USER_SETTINGS_TABS.map((t) => `/user-settings/${t}`))
    expect(list[0]).toMatchObject({ label: 'General', index: true })
  })

  it('hides Remote Machines when the build lacks the feature', () => {
    expect(shown(userSections({ remoteMachinesAvailable: false }))).toEqual([
      'General', 'Integrations', 'AI Engines', 'Audio', 'Usage',
    ])
  })
})

describe('the area paths', () => {
  it('isUserSettingsPath takes the page and its sections only', () => {
    expect(isUserSettingsPath('/user-settings')).toBe(true)
    expect(isUserSettingsPath('/user-settings/ai-engines')).toBe(true)
    expect(isUserSettingsPath('/user-settingsx')).toBe(false)
    expect(isUserSettingsPath('/chat/a')).toBe(false)
  })

  it('settingsBaseOf names the area, and the agents page is none', () => {
    expect(settingsBaseOf('/admin')).toBe('/admin')
    expect(settingsBaseOf('/admin/platform?tab=security')).toBe('/admin')
    expect(settingsBaseOf('/agents/ops')).toBe('/agents/ops')
    expect(settingsBaseOf('/agents/ops/config')).toBe('/agents/ops')
    expect(settingsBaseOf('/user-settings/usage')).toBe('/user-settings')
    expect(settingsBaseOf('/agents')).toBeNull()
    expect(settingsBaseOf('/agents?view=grid')).toBeNull()
    expect(settingsBaseOf('/chat/ops/c1')).toBeNull()
    expect(settingsBaseOf('/apps/a1')).toBeNull()
    expect(settingsBaseOf('/')).toBeNull()
  })

  it('currentSection takes the index on the base and the longest match below it', () => {
    const list = agentSections('ops', true)
    expect(currentSection(list, '/agents/ops', '/agents/ops')?.label).toBe('Overview')
    expect(currentSection(list, '/agents/ops', '/agents/ops/overview')?.label).toBe('Overview')
    expect(currentSection(list, '/agents/ops', '/agents/ops/triggers')?.label).toBe('Triggers')
    expect(currentSection(list, '/agents/ops', '/agents/ops/nowhere')).toBeNull()
  })
})

describe('userSettingsTabTarget (the ?tab= redirect)', () => {
  const extras: [string, string][] = [
    ['', ''],
    ['&provider=gmail-mcp', 'provider=gmail-mcp'],
    ['&wakeDiag=1', 'wakeDiag=1'],
    ['&provider=gmail-mcp&wakeDiag=1', 'provider=gmail-mcp&wakeDiag=1'],
  ]

  it('sends every tab to its section with every other parameter kept', () => {
    for (const tab of USER_SETTINGS_TABS) {
      for (const [inQuery, outQuery] of extras) {
        expect(userSettingsTabTarget(`?tab=${tab}${inQuery}`))
          .toBe(`/user-settings/${tab}${outQuery ? `?${outQuery}` : ''}`)
      }
    }
  })

  it('opens General for an unknown or empty tab, still keeping the rest', () => {
    expect(userSettingsTabTarget('?tab=billing')).toBe('/user-settings/general')
    expect(userSettingsTabTarget('?tab=')).toBe('/user-settings/general')
    expect(userSettingsTabTarget('?tab=billing&provider=x&wakeDiag=1'))
      .toBe('/user-settings/general?provider=x&wakeDiag=1')
  })

  it('keeps a parameter it does not know, in order', () => {
    expect(userSettingsTabTarget('?utm=a&tab=usage&z=2')).toBe('/user-settings/usage?utm=a&z=2')
  })
})
