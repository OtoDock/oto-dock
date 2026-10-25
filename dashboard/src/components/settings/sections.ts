import type { ComponentType } from 'react'
import {
  IconBell, IconBolt, IconChart, IconChats, IconChip, IconClipboardList, IconClock,
  IconDesktop, IconFlag, IconGear, IconInbox, IconLink, IconOverview, IconPhone,
  IconPuzzle, IconShare, IconShieldCheck, IconSliders, IconSparkles, IconSpeaker,
  IconUser, IconUsers, type NavIconProps,
} from '../icons/nav'
import { USER_SETTINGS_TABS } from '../../lib/openTarget'

/** One row of a settings area: the sidebar entry on a desktop, the list row
 *  on a phone. Gates and counts are resolved by the caller. */
export interface SettingsSection {
  label: string
  path: string
  icon: ComponentType<NavIconProps>
  /** Match the path exactly (NavLink `end`). */
  exact?: boolean
  /** False hides the row for this person or this build. */
  visible?: boolean
  /** A heading the row sits under; rows without one come first. */
  group?: string
  /** A count shown beside the label when above zero. */
  badge?: number
  /** The section the area's index shows on a desktop. */
  index?: boolean
}

export const ADMIN_BASE = '/admin'
export const USER_SETTINGS_BASE = '/user-settings'
const AGENT_PREFIX = '/agents/'
const MONITORING = 'Monitoring'

export function agentSettingsBase(name: string): string {
  return `${AGENT_PREFIX}${name}`
}

export function adminSections(opts: {
  remoteMachinesAvailable: boolean
  pendingMcpRequests: number
}): SettingsSection[] {
  const b = ADMIN_BASE
  return [
    { label: 'Overview', path: `${b}/overview`, icon: IconOverview, index: true },
    { label: 'Setup', path: `${b}/platform`, icon: IconGear },
    { label: 'Users', path: `${b}/users`, icon: IconUsers },
    { label: 'Usage', path: `${b}/usage`, icon: IconChart },
    { label: 'MCP Servers', path: `${b}/mcp-servers`, icon: IconPuzzle },
    { label: 'Skills', path: `${b}/skills`, icon: IconSparkles },
    { label: 'MCP Requests', path: `${b}/mcp-requests`, icon: IconInbox, badge: opts.pendingMcpRequests },
    // Hidden when this build ships without the remote-machines feature.
    { label: 'Remote Machines', path: `${b}/remote-machines`, icon: IconDesktop, visible: opts.remoteMachinesAvailable },
    { label: 'Shares', path: `${b}/shares`, icon: IconShare },
    { label: 'Reports', path: `${b}/reports`, icon: IconFlag },
    // Monitoring: the admin-audit views across every agent and person.
    { label: 'Scheduled Tasks', path: `${b}/scheduled-tasks`, icon: IconClock, group: MONITORING },
    { label: 'Triggers', path: `${b}/triggers`, icon: IconBolt, group: MONITORING },
    { label: 'Notifications', path: `${b}/notifications`, icon: IconBell, group: MONITORING },
    { label: 'Task History', path: `${b}/task-history`, icon: IconClipboardList, group: MONITORING },
    { label: 'Meetings', path: `${b}/meetings`, icon: IconChats, group: MONITORING },
  ]
}

export function agentSections(name: string, canManage: boolean): SettingsSection[] {
  const b = agentSettingsBase(name)
  return [
    { label: 'Overview', path: `${b}/overview`, icon: IconOverview, index: true },
    { label: 'MCPs', path: `${b}/mcps`, icon: IconPuzzle, visible: canManage },
    { label: 'Skills', path: `${b}/skills`, icon: IconSparkles, visible: canManage },
    { label: 'Configuration', path: `${b}/config`, icon: IconSliders, visible: canManage },
    // Everyone sees the offered checks and the verdicts they may see;
    // managers create and edit the agent's (CHECKS.md).
    { label: 'Checks', path: `${b}/checks`, icon: IconShieldCheck },
    // Monitoring: read-only operational views. Conversations are the
    // externally driven sessions (phone); dashboard chats live in the chat
    // sidebar. The pages gate their own actions.
    { label: 'Conversations', path: `${b}/conversations`, icon: IconPhone, visible: canManage, group: MONITORING },
    { label: 'Scheduled Tasks', path: `${b}/scheduled-tasks`, icon: IconClock, group: MONITORING },
    { label: 'Triggers', path: `${b}/triggers`, icon: IconBolt, group: MONITORING },
    { label: 'Notifications', path: `${b}/notifications`, icon: IconBell, group: MONITORING },
    { label: 'Meetings', path: `${b}/meetings`, icon: IconChats, group: MONITORING },
  ]
}

/** The User Settings sections' last path segments: the ids the `?tab=`
 *  links and the app SDK's `user_settings` target name. */
export type UserSettingsSectionId = (typeof USER_SETTINGS_TABS)[number]

const REMOTE_MACHINES: UserSettingsSectionId = 'remote-machines'

export function userSettingsPath(id: UserSettingsSectionId): string {
  return `${USER_SETTINGS_BASE}/${id}`
}

export function userSections(flags: { remoteMachinesAvailable: boolean }): SettingsSection[] {
  return [
    { label: 'General', path: userSettingsPath('general'), icon: IconUser, index: true },
    { label: 'Integrations', path: userSettingsPath('integrations'), icon: IconLink },
    // Hidden when this build ships without the feature; an admin who turned
    // user pairing off leaves the row and the section says so.
    { label: 'Remote Machines', path: userSettingsPath(REMOTE_MACHINES), icon: IconDesktop, visible: flags.remoteMachinesAvailable },
    { label: 'AI Engines', path: userSettingsPath('ai-engines'), icon: IconChip },
    { label: 'Audio', path: userSettingsPath('audio'), icon: IconSpeaker },
    { label: 'Usage', path: userSettingsPath('usage'), icon: IconChart },
  ]
}

function isUserSettingsSectionId(v: string | null): v is UserSettingsSectionId {
  return (USER_SETTINGS_TABS as readonly string[]).includes(v ?? '')
}

/** `/user-settings` and every section under it. */
export function isUserSettingsPath(pathname: string): boolean {
  return pathname === USER_SETTINGS_BASE || pathname.startsWith(`${USER_SETTINGS_BASE}/`)
}

/** Where `/user-settings?tab=<id>&…` goes: the section's own path with every
 *  other query parameter kept; an unknown id opens General. */
export function userSettingsTabTarget(search: string): string {
  const params = new URLSearchParams(search)
  const tab = params.get('tab')
  params.delete('tab')
  const id: UserSettingsSectionId = isUserSettingsSectionId(tab) ? tab : 'general'
  const rest = params.toString()
  return `${userSettingsPath(id)}${rest ? `?${rest}` : ''}`
}

/** The index path of the settings area a path belongs to (`/admin`,
 *  `/agents/<name>`, `/user-settings`), or null outside the three areas. The
 *  agents page itself (`/agents`) is not an area. */
export function settingsBaseOf(path: string): string | null {
  const pathname = path.split(/[?#]/, 1)[0]
  if (pathname === ADMIN_BASE || pathname.startsWith(`${ADMIN_BASE}/`)) return ADMIN_BASE
  if (isUserSettingsPath(pathname)) return USER_SETTINGS_BASE
  if (pathname.startsWith(AGENT_PREFIX)) {
    const name = pathname.slice(AGENT_PREFIX.length).split('/', 1)[0]
    return name ? agentSettingsBase(name) : null
  }
  return null
}

export interface SectionGroup {
  label: string
  items: SettingsSection[]
}

/** The visible sections, the ungrouped ones first, then each group in the
 *  order its first row appears. */
export function groupSections(sections: SettingsSection[]): { ungrouped: SettingsSection[]; groups: SectionGroup[] } {
  const ungrouped: SettingsSection[] = []
  const groups: SectionGroup[] = []
  for (const s of sections) {
    if (s.visible === false) continue
    if (!s.group) {
      ungrouped.push(s)
      continue
    }
    let g = groups.find((x) => x.label === s.group)
    if (!g) {
      g = { label: s.group, items: [] }
      groups.push(g)
    }
    g.items.push(s)
  }
  return { ungrouped, groups }
}

/** The visible section a path shows: the longest matching section path, or
 *  the index section on the area's own index. */
export function currentSection(
  sections: SettingsSection[], base: string, pathname: string,
): SettingsSection | null {
  if (pathname === base) return sections.find((s) => s.index) ?? null
  let best: SettingsSection | null = null
  for (const s of sections) {
    if (pathname === s.path || pathname.startsWith(`${s.path}/`)) {
      if (!best || s.path.length > best.path.length) best = s
    }
  }
  return best
}
