import { useEffect, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { apiFetch } from '../../api/auth'
import { SaveState, SettingRow } from '../../components/ui/SettingsControls'
import type { SaveStatus } from '../../hooks/useSaveState'
import { formatBytes } from './PlatformPage.format'
import { apiJson, withMessage } from '../../lib/http/apiJson'
import { useDialogs } from '../../components/ui/dialogs'
import { Input } from '../../components/ui/Field'
import { CARD } from '../../components/ui/classes'

// The archive of removed people (Setup → System Settings): when a person is
// deleted, their folders on every agent move into the archive. The sweep
// deletes an archive this many days after it was written (0 keeps them for
// ever, its own toggle); an admin lists them and purges one now. The two
// settings ride the shared platform-settings PUT.

interface OffboardedArchive {
  username: string
  agents: string[]
  /** null when the tree could not be measured. */
  bytes: number | null
  retired_at: string
  archived_at: string
  purge_after: string
}

interface OffboardedList {
  archives: OffboardedArchive[]
  retention: { enabled: boolean; days: number }
}

function useOffboardedArchives() {
  return useQuery({
    queryKey: ['offboarded-archives'],
    queryFn: async (): Promise<OffboardedList> => {
      return apiJson<OffboardedList>('/v1/admin/offboarded').catch(withMessage('Could not load the archive. Try again in a minute.'))
    },
  })
}

const day = (iso: string) => (iso ? new Date(iso).toLocaleDateString() : '')

export default function OffboardedArchivesCard({
  enabled, days, onEnabledChange, onDaysChange, onSaveDays, stateOf, forcedKeys,
}: {
  enabled: boolean
  days: string
  onEnabledChange: (v: boolean) => void
  onDaysChange: (v: string) => void
  onSaveDays: () => void
  /** The tab's save state per setting (`useSaveState().stateOf`). */
  stateOf: (key: string) => SaveStatus
  forcedKeys: string[]
}) {
  const dialogs = useDialogs()
  const qc = useQueryClient()
  const { data, isLoading, error } = useOffboardedArchives()
  const [notice, setNotice] = useState<string | null>(null)
  const purge = useMutation({
    mutationFn: async (username: string) => {
      const res = await apiFetch(`/v1/admin/offboarded/${encodeURIComponent(username)}`, { method: 'DELETE' })
      const body = await res.json().catch(() => ({}))
      if (!res.ok) throw new Error(body.detail || 'The archive was not deleted. Try again.')
      return body as { username: string; bytes: number }
    },
    onSuccess: (r) => {
      setNotice(`Deleted the archive of ${r.username}. It freed ${formatBytes(r.bytes)}.`)
      qc.invalidateQueries({ queryKey: ['offboarded-archives'] })
      qc.invalidateQueries({ queryKey: ['storage-usage'] })
    },
    onError: (e: Error) => setNotice(e.message),
  })
  // Each row's "deleted after" date follows the settings: re-read the list
  // once a change to them is saved.
  const settingsSaved = stateOf('offboarded_retention_enabled') === 'saved'
    || stateOf('offboarded_retention_days') === 'saved'
  useEffect(() => {
    if (settingsSaved) qc.invalidateQueries({ queryKey: ['offboarded-archives'] })
  }, [settingsSaved, qc])
  const enabledForced = forcedKeys.includes('offboarded_retention_enabled')
  const daysForced = forcedKeys.includes('offboarded_retention_days')

  return (
    <div className={`${CARD} p-5 space-y-5`}>
      <h3 className="text-sm font-semibold text-primary">Archive of removed people</h3>

      <SettingRow
        label="Delete old archives"
        htmlFor="offboarded-retention-enabled"
        save={stateOf('offboarded_retention_enabled')}
        description="With this on, the daily cleanup deletes archives older than the period below."
        info={<>
          When a person is removed, their folders on every agent move into an archive.
          Only an admin can reach it.
        </>}
      >
        <input
          id="offboarded-retention-enabled"
          type="checkbox"
          aria-label="Delete old archives"
          checked={enabled}
          disabled={enabledForced}
          onChange={(e) => onEnabledChange(e.target.checked)}
          className="h-4 w-4 accent-action"
        />
      </SettingRow>

      <div className="flex items-center justify-between gap-4">
        <div className="flex-1 min-w-0">
          <p className="text-sm font-medium text-primary mb-0.5">
            <label>Days to keep archives</label><SaveState state={stateOf('offboarded_retention_days')} />
          </p>
          <p className="text-xs text-tertiary">0 keeps every archive for ever.</p>
        </div>
        <div className="flex items-center gap-2 shrink-0">
          <Input
            type="number"
            aria-label="Days to keep archives"
            value={days}
            onChange={(e) => onDaysChange(e.target.value)}
            onBlur={onSaveDays}
            min={0}
            max={3650}
            disabled={!enabled || daysForced}
            className="max-w-20 text-right"
          />
        </div>
      </div>

      <div>
        <p className="text-sm font-medium text-primary mb-2">Archived people</p>
        {isLoading ? (
          <p className="text-xs text-tertiary">Loading…</p>
        ) : error ? (
          <p className="text-xs text-status-danger-fg">{(error as Error).message}</p>
        ) : !data?.archives.length ? (
          <p className="text-xs text-tertiary">No archives.</p>
        ) : (
          <ul className="divide-y divide-subtle">
            {data.archives.map((a) => (
              <li key={a.username} className="flex items-center justify-between gap-3 py-2 text-xs">
                <div className="min-w-0">
                  <p className="text-sm text-primary truncate">{a.username}</p>
                  <p className="text-tertiary truncate">
                    {a.agents.length} agent folder{a.agents.length === 1 ? '' : 's'} · {a.bytes == null ? 'size unknown' : formatBytes(a.bytes)}
                    {a.archived_at ? ` · archived ${day(a.archived_at)}`
                      : a.retired_at ? ' · still being archived' : ' · undated, the person was restored'}
                    {' · '}{a.purge_after ? `deleted after ${day(a.purge_after)}` : 'kept'}
                  </p>
                </div>
                <button
                  onClick={async () => {
                    if (await dialogs.confirm({
                      title: `Delete the archive of ${a.username} now?`,
                      body: 'Their files on every agent go for good.',
                      confirmLabel: 'Delete',
                      tone: 'danger',
                    })) {
                      setNotice(null)
                      purge.mutate(a.username)
                    }
                  }}
                  disabled={purge.isPending}
                  aria-label={`Delete the archive of ${a.username} now`}
                  className="shrink-0 px-2.5 py-1 text-xs font-medium border border-status-danger-border text-status-danger-fg rounded-control hover:bg-status-danger-bg disabled:opacity-50"
                >
                  Delete now
                </button>
              </li>
            ))}
          </ul>
        )}
        {notice && <p className="text-xs text-secondary mt-2">{notice}</p>}
      </div>
    </div>
  )
}
