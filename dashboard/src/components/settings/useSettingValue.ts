/**
 * The value behind a settings card in the chat (components/chat/SettingCard),
 * beside the catalog it reads:
 * read and saved with the viewer's own sign-in through the catalog row's
 * binding (components/settings/catalog.ts). A REST binding reads the GET's
 * JSON field under the settings page's own query key and writes
 * `{[field]: value}`, so a save on the page, in the chat's microphone menu
 * or in the card shows everywhere at once. The profile reads the signed-in
 * user and refreshes it after the save. A device binding is this browser's
 * theme or chat-activity pick, saved at once with no round trip. A platform
 * setting the operator forced, or owns on a cloud install, is `managed`.
 * Whether the viewer changes the row at all is the server's answer
 * (`useSettingCard`), never a guess from the role the dashboard holds.
 */
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useCallback } from 'react'

import { fetchCurrentUser } from '../../api/auth'
import { useAuth } from '../../contexts/AuthContext'
import { useTheme, type Theme } from '../../contexts/useTheme'
import { setActivityDisplay, useActivityDisplay, type ActivityDisplayMode } from '../../hooks/useActivityDisplay'
import { apiJson, apiOk } from '../../lib/http/apiJson'
import type { CatalogEntry, RestBinding } from './catalog'

export type SettingValue = boolean | string | number | null

/** The settings pages' query keys: the card reads through them and
 *  refreshes them after a save (api/userUiPrefs, api/userAudio,
 *  pages/admin/PlatformPage.hooks). */
const PAGE_KEYS: Record<string, string[]> = {
  '/v1/users/me/ui-prefs': ['my-ui-prefs'],
  '/v1/users/me/audio-prefs': ['my-audio-prefs'],
  '/v1/admin/platform-settings': ['platform-settings'],
}

const ME = '/auth/me'
const PLATFORM_SETTINGS = '/v1/admin/platform-settings'

export interface SettingValueState {
  value: SettingValue
  /** The value is still being read. */
  loading: boolean
  /** Kept in this browser: saved at once, no Save button. */
  device: boolean
  /** The operator manages it: forced on this install, or owned on the cloud. */
  managed: boolean
  save: (next: SettingValue) => Promise<unknown>
}

function fieldOf(data: unknown, field: string): SettingValue {
  if (data && typeof data === 'object' && field in data) {
    const v = (data as Record<string, unknown>)[field]
    if (typeof v === 'boolean' || typeof v === 'string' || typeof v === 'number' || v === null) return v
  }
  return null
}

export function useSettingValue(entry: CatalogEntry | undefined): SettingValueState {
  const qc = useQueryClient()
  const { user, setUser } = useAuth()
  const { theme, setTheme } = useTheme()
  const activity = useActivityDisplay()
  const binding = entry?.value
  const rest: RestBinding | null = binding && 'read' in binding ? binding : null
  const device = binding && 'device' in binding ? binding.device : null
  const readPath = rest?.read.path ?? ''
  const viaMe = readPath === ME
  const queryKey = PAGE_KEYS[readPath] ?? ['setting-value', readPath]

  const query = useQuery({
    queryKey,
    queryFn: () => apiJson<unknown>(readPath),
    enabled: Boolean(rest) && !viaMe,
  })

  const mutation = useMutation({
    mutationFn: async (next: SettingValue) => {
      if (!rest) return
      const init = { method: rest.write.method ?? 'PUT', body: JSON.stringify({ [rest.write.field]: next }) }
      if (viaMe) {
        await apiOk(rest.write.path, init, 'The setting was not saved.')
        const u = await fetchCurrentUser()
        if (u) setUser(u)
        return
      }
      await apiJson(rest.write.path, init, 'The setting was not saved.')
    },
    onSuccess: async () => {
      if (!rest) return
      await qc.invalidateQueries({ queryKey })
      const keys = PAGE_KEYS[rest.write.path]
      if (keys && rest.write.path !== readPath) await qc.invalidateQueries({ queryKey: keys })
    },
  })

  const { mutateAsync } = mutation
  const save = useCallback(async (next: SettingValue) => {
    if (device === 'theme') { setTheme(String(next) as Theme); return }
    if (device === 'chat-activity') { setActivityDisplay(String(next) as ActivityDisplayMode); return }
    return mutateAsync(next)
  }, [device, mutateAsync, setTheme])

  let value: SettingValue = null
  let loading = false
  let managed = false
  if (device === 'theme') value = theme
  else if (device === 'chat-activity') value = activity
  else if (rest && viaMe) value = fieldOf(user, rest.read.field)
  else if (rest) {
    loading = query.isLoading
    value = query.data === undefined ? null : fieldOf(query.data, rest.read.field)
    if (readPath === PLATFORM_SETTINGS && query.data) managed = managedBy(query.data, rest)
  }
  if (value === null && rest && rest.default !== undefined) value = rest.default

  return { value, loading, device: device !== null, managed, save }
}

/** The platform settings read also says which keys the operator forced and
 *  whether this is a cloud install, where the operator owns the marked rows. */
function managedBy(data: unknown, rest: RestBinding): boolean {
  const d = data as { forced_keys?: unknown; cloud?: unknown }
  const forced = Array.isArray(d.forced_keys) && d.forced_keys.includes(rest.read.field)
  return forced || (Boolean(d.cloud) && Boolean(rest.cloudManaged))
}

/** The server's answer for this viewer: the row's card exists and their own
 *  role changes it. A refusal (an admin's row and a viewer below admin) is
 *  an ApiError whose message is the server's sentence. */
export function useSettingCard(settingId: string, enabled: boolean) {
  return useQuery({
    queryKey: ['setting-card', settingId],
    queryFn: () => apiJson<{ setting: string; label: string; changes: boolean }>(
      `/v1/settings/card/${encodeURIComponent(settingId)}`),
    enabled,
    retry: false,
  })
}
