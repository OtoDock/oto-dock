import type { CatalogEntry, SettingsArea } from './catalog.ts'

/**
 * Where a catalog row lives, as a person is told: the areas' and the
 * sections' names as the pages show them (the catalog test holds them
 * equal to the section registry's labels). The settings map generator and
 * the card in the chat read them. A leaf like the catalog, so Node reads
 * it with type stripping.
 */

export const AREA_LABELS: Record<SettingsArea, string> = { user: 'User Settings', agent: 'Agent Settings', admin: 'Admin' }

export const SECTION_LABELS: Record<SettingsArea, Record<string, string>> = {
  user: {
    general: 'General', integrations: 'Integrations', 'remote-machines': 'Remote Machines',
    'ai-engines': 'AI Engines', audio: 'Audio', usage: 'Usage',
  },
  agent: {},
  admin: {
    overview: 'Overview', platform: 'Setup', users: 'Users', usage: 'Usage', 'mcp-servers': 'MCP Servers',
    skills: 'Skills', 'mcp-requests': 'MCP Requests', 'remote-machines': 'Remote Machines', shares: 'Shares',
    reports: 'Reports', 'scheduled-tasks': 'Scheduled Tasks', triggers: 'Triggers', notifications: 'Notifications',
    'task-history': 'Task History', meetings: 'Meetings',
  },
}

/** The area, the section and the tab: "User Settings, General". */
export function whereOf(entry: Pick<CatalogEntry, 'area' | 'section' | 'tab'>): string {
  const parts = [AREA_LABELS[entry.area], SECTION_LABELS[entry.area][entry.section] ?? entry.section]
  if (entry.tab) parts.push(entry.tab)
  return parts.join(', ')
}

/** The line under a card's label: where the row lives and, when the
 *  heading says more than the label, the heading it sits under. */
export function whereLine(entry: Pick<CatalogEntry, 'area' | 'section' | 'tab' | 'group' | 'label'>): string {
  const under = entry.group && entry.group !== entry.label ? `, under ${entry.group}` : ''
  return `${whereOf(entry)}${under}.`
}
