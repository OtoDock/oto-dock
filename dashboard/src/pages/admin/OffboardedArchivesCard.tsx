import { useEffect, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { apiFetch } from '../../api/auth'
import { SavedBadge, formatBytes } from './PlatformPage.shared'

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
      const res = await apiFetch('/v1/admin/offboarded')
      if (!res.ok) throw new Error('Failed to list the archive')
      return res.json()
    },
  })
}

const day = (iso: string) => (iso ? new Date(iso).toLocaleDateString() : '')

export default function OffboardedArchivesCard({
  enabled, days, onEnabledChange, onDaysChange, onSaveDays, savedField, daysError, forcedKeys,
}: {
  enabled: boolean
  days: string
  onEnabledChange: (v: boolean) => void
  onDaysChange: (v: string) => void
  onSaveDays: () => void
  savedField: string
  daysError: string | null
  forcedKeys: string[]
}) {
  const qc = useQueryClient()
  const { data, isLoading, error } = useOffboardedArchives()
  const [notice, setNotice] = useState<string | null>(null)
  const purge = useMutation({
    mutationFn: async (username: string) => {
      const res = await apiFetch(`/v1/admin/offboarded/${encodeURIComponent(username)}`, { method: 'DELETE' })
      const body = await res.json().catch(() => ({}))
      if (!res.ok) throw new Error(body.detail || 'Purge failed')
      return body as { username: string; bytes: number }
    },
    onSuccess: (r) => {
      setNotice(`Deleted the archive of ${r.username} (${formatBytes(r.bytes)})`)
      qc.invalidateQueries({ queryKey: ['offboarded-archives'] })
      qc.invalidateQueries({ queryKey: ['storage-usage'] })
    },
    onError: (e: Error) => setNotice(e.message),
  })
  // Each row's "deleted after" date follows the settings: re-read the list
  // once a change to them is saved.
  useEffect(() => {
    if (savedField === 'offboarded_retention_enabled' || savedField === 'offboarded_retention_days') {
      qc.invalidateQueries({ queryKey: ['offboarded-archives'] })
    }
  }, [savedField, qc])
  const enabledForced = forcedKeys.includes('offboarded_retention_enabled')
  const daysForced = forcedKeys.includes('offboarded_retention_days')

  return (
    <div className="bg-white dark:bg-p-surface rounded-xl border border-p-border-light p-5 space-y-5">
      <h3 className="text-sm font-semibold text-p-text">Archive of removed people</h3>

      <div className="flex items-center justify-between gap-4">
        <div className="flex-1">
          <label className="block text-sm font-medium text-p-text mb-0.5">Delete old archives</label>
          <p className="text-xs text-p-text-light">
            When a person is removed, their folders on every agent move into an archive only an
            admin reaches. With this on, the daily cleanup deletes an archive once it is older
            than the period below.
          </p>
        </div>
        <div className="flex items-center gap-2">
          <input
            type="checkbox"
            aria-label="Delete old archives"
            checked={enabled}
            disabled={enabledForced}
            onChange={(e) => onEnabledChange(e.target.checked)}
            className="h-4 w-4 text-brand rounded-sm focus:ring-2 focus:ring-brand/30"
          />
          <SavedBadge show={savedField === 'offboarded_retention_enabled'} />
        </div>
      </div>

      <div className="flex items-center justify-between gap-4">
        <div className="flex-1 min-w-0">
          <label className="block text-sm font-medium text-p-text mb-0.5">Keep archives for (days)</label>
          <p className="text-xs text-p-text-light">0 keeps every archive for ever.</p>
          {daysError && <p className="text-xs text-p-accent-red mt-0.5">{daysError}</p>}
        </div>
        <div className="flex items-center gap-2 shrink-0">
          <input
            type="number"
            aria-label="Keep archives for (days)"
            value={days}
            onChange={(e) => onDaysChange(e.target.value)}
            onBlur={onSaveDays}
            min={0}
            max={3650}
            disabled={!enabled || daysForced}
            className="w-20 px-2 py-1.5 text-sm border border-p-border-light rounded-lg bg-p-bg text-p-text focus:outline-hidden focus:ring-2 focus:ring-brand/30 text-right disabled:opacity-50"
          />
          <SavedBadge show={savedField === 'offboarded_retention_days'} />
        </div>
      </div>

      <div>
        <p className="text-sm font-medium text-p-text mb-2">Archived people</p>
        {isLoading ? (
          <p className="text-xs text-p-text-light">Loading…</p>
        ) : error ? (
          <p className="text-xs text-p-accent-red">{(error as Error).message}</p>
        ) : !data?.archives.length ? (
          <p className="text-xs text-p-text-light">No archives.</p>
        ) : (
          <ul className="divide-y divide-p-border-light">
            {data.archives.map((a) => (
              <li key={a.username} className="flex items-center justify-between gap-3 py-2 text-xs">
                <div className="min-w-0">
                  <p className="text-sm text-p-text truncate">{a.username}</p>
                  <p className="text-p-text-light truncate">
                    {a.agents.length} agent folder{a.agents.length === 1 ? '' : 's'} · {a.bytes == null ? 'size unknown' : formatBytes(a.bytes)}
                    {a.archived_at ? ` · archived ${day(a.archived_at)}`
                      : a.retired_at ? ' · still being archived' : ' · undated (the person was restored)'}
                    {' · '}{a.purge_after ? `deleted after ${day(a.purge_after)}` : 'kept'}
                  </p>
                </div>
                <button
                  onClick={() => {
                    if (window.confirm(`Delete the archive of ${a.username} now? Their files on every agent go for good.`)) {
                      setNotice(null)
                      purge.mutate(a.username)
                    }
                  }}
                  disabled={purge.isPending}
                  aria-label={`Delete the archive of ${a.username} now`}
                  className="shrink-0 px-2.5 py-1 text-xs font-medium border border-red-300 text-red-600 rounded-lg hover:bg-red-50 dark:hover:bg-red-900/20 disabled:opacity-50"
                >
                  Delete now
                </button>
              </li>
            ))}
          </ul>
        )}
        {notice && <p className="text-xs text-p-text-secondary mt-2">{notice}</p>}
      </div>
    </div>
  )
}
