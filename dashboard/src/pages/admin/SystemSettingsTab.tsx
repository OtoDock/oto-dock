import { useState, useEffect } from 'react'
import { useQueryClient } from '@tanstack/react-query'
import { apiFetch } from '../../api/auth'
import { useAuth } from '../../contexts/AuthContext'
import { useTitleGeneration, useSaveTitleGeneration } from '../../api/titleGeneration'
import { useMcpAutoUpdateLog, type McpAutoUpdateRow } from '../../api/mcps'
import { useMemorySettings, useUpdateMemorySettings } from '../../api/memory'
import {
  usePlatformSettings,
  useSavePlatformSettings,
  useConcurrencyStats,
  useStorageUsage,
} from './PlatformPage.hooks'
import {
  SavedBadge,
  ConcurrencyRow,
  QuotaRow,
  formatBytes,
  relativeTime,
  TIMEZONE_OPTIONS,
  MCP_STATUS_LABEL,
} from './PlatformPage.shared'

// ---------------------------------------------------------------------------
// Storage & Retention card (Setup → System Settings). Settings ride the shared
// platform-settings PUT; usage + run-now hit api/admin/admin_storage.py.
// ---------------------------------------------------------------------------

function StorageRetentionCard({
  enabled, days, onEnabledChange, onDaysChange, onSaveDays, savedField,
}: {
  enabled: boolean
  days: string
  onEnabledChange: (v: boolean) => void
  onDaysChange: (v: string) => void
  onSaveDays: () => void
  savedField: string
}) {
  const qc = useQueryClient()
  const { data: usage } = useStorageUsage()
  const [running, setRunning] = useState(false)
  const [result, setResult] = useState<string | null>(null)
  const [resultOk, setResultOk] = useState(true)

  const runNow = async (dryRun: boolean) => {
    setRunning(true); setResult(null)
    try {
      const res = await apiFetch('/v1/admin/retention/run-now', {
        method: 'POST', body: JSON.stringify({ dry_run: dryRun }),
      })
      if (!res.ok) throw new Error('Request failed')
      const d = await res.json()
      const freed = (d.bytes_freed || 0) + (d.orphan_bytes || 0)
        + (d.codex_junk_bytes || 0) + (d.tarball_bytes || 0)
      setResultOk(true)
      setResult(
        `${dryRun ? 'Would free' : 'Freed'} ${formatBytes(freed)} · `
        + `${d.chats_flagged ?? 0} chats aged out · ${d.orphans_deleted ?? 0} orphans · `
        + `${d.codex_junk_files ?? 0} junk files${d.errors ? ` · ${d.errors} errors` : ''}`,
      )
      if (!dryRun) qc.invalidateQueries({ queryKey: ['storage-usage'] })
    } catch {
      setResultOk(false)
      setResult('Cleanup request failed')
    } finally {
      setRunning(false)
    }
  }

  const last = usage?.retention?.last_sweep
  const usageRows: Array<[string, number | undefined]> = [
    ['Agents folder (total)', usage?.agents_bytes],
    ['Session files', usage?.session_files_bytes],
    ['Codex junk', usage?.codex_junk_bytes],
    ['Recover bin', usage?.recover_bin_bytes],
    ['Proxy cache', usage?.sessions_dir_bytes],
    ['Logs', usage?.logs_bytes],
  ]

  return (
    <div className="bg-white dark:bg-p-surface rounded-xl border border-p-border-light p-5 space-y-5">
      <h3 className="text-sm font-semibold text-p-text">Storage & Retention</h3>

      <div className="flex items-center justify-between gap-4">
        <div className="flex-1">
          <label className="block text-sm font-medium text-p-text mb-0.5">Clean up old chat session files</label>
          <p className="text-xs text-p-text-light">
            Chats on local agents untouched for the period below lose their on-disk CLI session files.
            Their full history stays in the database — reopening one continues with a fresh session
            seeded from that history.
          </p>
        </div>
        <div className="flex items-center gap-2">
          <input
            type="checkbox"
            checked={enabled}
            onChange={(e) => onEnabledChange(e.target.checked)}
            className="h-4 w-4 text-brand rounded-sm focus:ring-2 focus:ring-brand/30"
          />
          <SavedBadge show={savedField === 'session_retention_enabled'} />
        </div>
      </div>

      <div className="flex items-center justify-between gap-4">
        <div className="flex-1 min-w-0">
          <label className="block text-sm font-medium text-p-text mb-0.5">Keep session files for (days)</label>
          <p className="text-xs text-p-text-light">Sessions idle longer than this age out on the daily sweep. Minimum 7.</p>
        </div>
        <div className="flex items-center gap-2 shrink-0">
          <input
            type="number"
            value={days}
            onChange={(e) => onDaysChange(e.target.value)}
            onBlur={onSaveDays}
            min={7}
            disabled={!enabled}
            className="w-20 px-2 py-1.5 text-sm border border-p-border-light rounded-lg bg-p-bg text-p-text focus:outline-hidden focus:ring-2 focus:ring-brand/30 text-right disabled:opacity-50"
          />
          <SavedBadge show={savedField === 'session_retention_days'} />
        </div>
      </div>

      <div>
        <p className="text-sm font-medium text-p-text mb-2">Disk usage</p>
        <div className="grid grid-cols-2 sm:grid-cols-3 gap-x-6 gap-y-1">
          {usageRows.map(([label, bytes]) => (
            <div key={label} className="flex items-center justify-between gap-2 text-xs">
              <span className="text-p-text-light truncate">{label}</span>
              <span className="text-p-text tabular-nums shrink-0">{usage ? formatBytes(bytes) : '…'}</span>
            </div>
          ))}
        </div>
        {last && (
          <p className="text-xs text-p-text-light mt-2">
            Last cleanup: {new Date(String(last.ran_at)).toLocaleString()} —
            freed {formatBytes(
              (Number(last.bytes_freed) || 0) + (Number(last.orphan_bytes) || 0)
              + (Number(last.codex_junk_bytes) || 0) + (Number(last.tarball_bytes) || 0),
            )}
          </p>
        )}
      </div>

      <div className="flex items-center gap-3 flex-wrap">
        <button
          onClick={() => runNow(false)}
          disabled={running}
          className="px-3 py-1.5 text-xs font-medium rounded-lg bg-brand text-white hover:bg-brand-hover transition-colors disabled:opacity-40"
        >
          {running ? 'Running…' : 'Run cleanup now'}
        </button>
        <button
          onClick={() => runNow(true)}
          disabled={running}
          className="px-3 py-1.5 text-xs font-medium border border-p-border-light rounded-lg text-p-text-secondary hover:bg-p-surface-hover disabled:opacity-50"
        >
          Dry run
        </button>
        {result && (
          <span className={`text-xs font-medium ${resultOk ? 'text-green-600 dark:text-green-400' : 'text-p-accent-red'}`}>
            {result}
          </span>
        )}
      </div>
    </div>
  )
}
// ---------------------------------------------------------------------------
// Storage Quotas card (Setup → System Settings). Per-agent disk limits on local agent
// folders; settings ride the shared platform-settings PUT. Enforcement is hard
// (XFS) only when the kernel tier is on — otherwise measurement + warnings.
// ---------------------------------------------------------------------------
function StorageQuotasCard({
  values, enforced, forcedKeys, onChange, onSave, savedField,
}: {
  values: Record<string, string>
  enforced: boolean
  forcedKeys: string[]
  onChange: (k: string, v: string) => void
  onSave: (k: string) => void
  savedField: string
}) {
  const isForced = (k: string) => forcedKeys?.includes(k)
  const gbHint = (mb: string) => {
    const n = Number(mb)
    if (!Number.isFinite(n) || n <= 0) return 'Unlimited'
    return n % 1024 === 0 ? `${n / 1024} GB` : `${(n / 1024).toFixed(1)} GB`
  }
  const inodeHint = (v: string) => (Number(v) > 0 ? `${Number(v).toLocaleString()} files` : 'Unlimited')

  return (
    <div className="bg-white dark:bg-p-surface rounded-xl border border-p-border-light p-5 space-y-5">
      <div className="flex items-center justify-between gap-3">
        <h3 className="text-sm font-semibold text-p-text">Storage Quotas</h3>
        <span className={`text-xs font-medium px-2 py-0.5 rounded-full shrink-0 ${
          enforced
            ? 'bg-green-100 text-green-700 dark:bg-green-900/40 dark:text-green-300'
            : 'bg-p-surface-hover text-p-text-light'
        }`}>
          {enforced ? 'Hard enforcement (XFS)' : 'Measurement & warnings'}
        </span>
      </div>

      <p className="text-xs text-p-text-light">
        Per-agent disk limits on local agent folders. The owner (personal folder) or the
        managers + editors (shared folder) get a warning notification at 90 / 95 / 100%.
        Set a limit to <span className="font-medium">0</span> for unlimited.{' '}
        {enforced
          ? 'Hard enforcement is active: writes past a limit are blocked at the kernel (EDQUOT), and remote machines inherit the boundary — a sync-back into a full folder fails and retries later.'
          : 'Measurement + warnings only — hard enforcement auto-activates when the agents data dir is on an XFS volume with project quota.'}
      </p>

      <QuotaRow
        label="Shared agent folder"
        desc="workspace + knowledge + config, summed"
        hint={gbHint(values.quota_shared_folder_mb)}
        unit="MB"
        value={values.quota_shared_folder_mb}
        settingKey="quota_shared_folder_mb"
        forced={isForced('quota_shared_folder_mb')}
        onChange={(v) => onChange('quota_shared_folder_mb', v)}
        onSave={() => onSave('quota_shared_folder_mb')}
        savedField={savedField}
      />
      <QuotaRow
        label="Per-user folder"
        desc="each user's personal users/{name}/ folder"
        hint={gbHint(values.quota_user_folder_mb)}
        unit="MB"
        value={values.quota_user_folder_mb}
        settingKey="quota_user_folder_mb"
        forced={isForced('quota_user_folder_mb')}
        onChange={(v) => onChange('quota_user_folder_mb', v)}
        onSave={() => onSave('quota_user_folder_mb')}
        savedField={savedField}
      />

      <details className="group">
        <summary className="text-xs font-medium text-p-text-secondary cursor-pointer select-none hover:text-p-text">
          File-count limits (advanced — default off)
        </summary>
        <div className="mt-3 space-y-4">
          <QuotaRow
            label="Shared folder file limit"
            desc="max files in the shared agent folder"
            hint={inodeHint(values.quota_shared_folder_inodes)}
            unit="files"
            value={values.quota_shared_folder_inodes}
            settingKey="quota_shared_folder_inodes"
            forced={isForced('quota_shared_folder_inodes')}
            onChange={(v) => onChange('quota_shared_folder_inodes', v)}
            onSave={() => onSave('quota_shared_folder_inodes')}
            savedField={savedField}
          />
          <QuotaRow
            label="Per-user folder file limit"
            desc="max files in each user's personal folder"
            hint={inodeHint(values.quota_user_folder_inodes)}
            unit="files"
            value={values.quota_user_folder_inodes}
            settingKey="quota_user_folder_inodes"
            forced={isForced('quota_user_folder_inodes')}
            onChange={(v) => onChange('quota_user_folder_inodes', v)}
            onSave={() => onSave('quota_user_folder_inodes')}
            savedField={savedField}
          />
        </div>
      </details>
    </div>
  )
}
// Chat-title generation — provider/model sourced from the Direct LLM execution
// layer (replaces the old per-key OpenAI title path). Toggle + model dropdown +
// Active/Inactive badge, mirroring the phone Turn-Classifier card.
function ChatTitleGenerationCard() {
  const { data: tg, isLoading } = useTitleGeneration()
  const saveMutation = useSaveTitleGeneration()

  if (isLoading || !tg) {
    return (
      <div className="bg-white dark:bg-p-surface rounded-xl border border-p-border-light p-5">
        <p className="text-sm text-p-text-secondary">Loading...</p>
      </div>
    )
  }

  const autoLabel = (!tg.selected_model && tg.active_model)
    ? `Auto — ${tg.active_model}`
    : 'Auto (first configured provider)'

  return (
    <div className="bg-white dark:bg-p-surface rounded-xl border border-p-border-light p-5 space-y-4">
      <div className="flex items-center justify-between">
        <h3 className="text-sm font-semibold text-p-text">Chat Title Generation</h3>
        <span className={`text-xs px-2 py-0.5 rounded-full ${
          tg.active
            ? 'bg-green-100 text-green-700 dark:bg-green-900/30 dark:text-green-400'
            : 'bg-p-bg text-p-text-light border border-p-border-light'
        }`}>
          {tg.active ? 'Active' : 'Inactive'}
        </span>
      </div>
      <p className="text-xs text-p-text-light">
        Upgrades a new dashboard chat's title to a concise, emoji-prefixed summary
        generated from the first message and reply. Provider and model come from the
        Direct LLM AI engine — no separate API key.
      </p>

      {/* Enable toggle */}
      <div className="flex items-center justify-between gap-4">
        <div className="flex-1">
          <label className="block text-sm font-medium text-p-text mb-0.5">Enabled</label>
          <p className="text-xs text-p-text-light">
            When off, titles stay as the first words of the prompt.
          </p>
        </div>
        <input
          type="checkbox"
          checked={tg.enabled}
          onChange={(e) => saveMutation.mutate({ enabled: e.target.checked })}
          disabled={saveMutation.isPending}
          className="h-4 w-4 text-brand rounded-sm focus:ring-2 focus:ring-brand/30"
        />
      </div>

      {/* Model — flex-wrap + w-full on mobile: the long "label — model" option
          text otherwise forces the select past the page width */}
      <div className="flex flex-wrap items-center justify-between gap-4">
        <div className="flex-1">
          <label className="block text-sm font-medium text-p-text mb-0.5">Model</label>
        </div>
        <select
          value={tg.selected_model}
          onChange={(e) => saveMutation.mutate({ model: e.target.value })}
          disabled={!tg.enabled || saveMutation.isPending}
          className="w-full sm:w-auto min-w-0 max-w-full px-2.5 py-1.5 text-sm border border-p-border-light rounded-lg bg-p-bg text-p-text focus:outline-hidden focus:ring-2 focus:ring-brand/30 disabled:opacity-50"
        >
          <option value="">{autoLabel}</option>
          {tg.options.map((o) => (
            <option key={o.model} value={o.model}>{o.label} — {o.model}</option>
          ))}
        </select>
      </div>

      {!tg.active && tg.enabled && (
        <p className="text-xs text-amber-700 dark:text-amber-400">
          No Direct LLM provider configured — add one under AI Engines → Direct LLM to activate.
        </p>
      )}
    </div>
  )
}
// Automatic MCP updates — toggle + quiet status line + inline run history.
// The toggle is part of platform-settings (saved by GeneralTab); the history
// comes from its own endpoint.
function McpAutoUpdateCard({ enabled, forced, saved, onToggle }: {
  enabled: boolean
  forced: boolean
  saved: boolean
  onToggle: (v: boolean) => void
}) {
  const { data } = useMcpAutoUpdateLog()
  const runs = data?.runs || []
  const lastRunAt = data?.last_run_at || ''

  // Counts for the most recent run (rows arrive newest-first).
  const latestRunId = runs[0]?.run_id
  const latest = latestRunId ? runs.filter((r) => r.run_id === latestRunId) : []
  const count = (s: string) => latest.filter((r) => r.status === s).length

  let statusLine = 'Has not run yet — first run lands in the next weekly window.'
  if (lastRunAt) {
    if (latest.length) {
      const parts = [
        `${count('updated')} updated`,
        count('failed') ? `${count('failed')} failed` : null,
        count('skipped_in_use') ? `${count('skipped_in_use')} skipped (in use)` : null,
        count('held') ? `${count('held')} held` : null,
      ].filter(Boolean)
      statusLine = `Last run ${relativeTime(lastRunAt)} — ${parts.join(', ')}.`
    } else {
      statusLine = `Last run ${relativeTime(lastRunAt)} — everything was up to date.`
    }
  }

  return (
    <div className="bg-white dark:bg-p-surface rounded-xl border border-p-border-light p-5 space-y-4">
      <h3 className="text-sm font-semibold text-p-text">Automatic MCP Updates</h3>
      <p className="text-xs text-p-text-light">
        Once a week, in a low-traffic window, automatically apply available updates to
        community MCPs. Docker MCPs in active use are deferred until free; only failures
        notify admins.
      </p>

      <div className="flex items-center justify-between gap-4">
        <div className="flex-1">
          <label className="block text-sm font-medium text-p-text mb-0.5">Enabled</label>
          <p className="text-xs text-p-text-light">{statusLine}</p>
        </div>
        <div className="flex items-center gap-2">
          <input
            type="checkbox"
            checked={enabled}
            disabled={forced}
            onChange={(e) => onToggle(e.target.checked)}
            className="h-4 w-4 text-brand rounded-sm focus:ring-2 focus:ring-brand/30 disabled:opacity-50 disabled:cursor-not-allowed"
          />
          <SavedBadge show={saved} />
        </div>
      </div>

      {runs.length > 0 && (
        <details className="group">
          <summary className="text-xs font-medium text-p-text-secondary cursor-pointer select-none hover:text-p-text">
            Recent updates ({runs.length})
          </summary>
          <div className="mt-3 space-y-1.5">
            {runs.map((r: McpAutoUpdateRow, i: number) => (
              <div key={i} className="flex items-center justify-between gap-3 text-xs">
                <span className="text-p-text font-medium truncate">{r.mcp_name}</span>
                <span className={`whitespace-nowrap ${r.status === 'failed' ? 'text-red-600 dark:text-red-400' : 'text-p-text-light'}`}>
                  {r.status === 'updated' && r.old_version && r.new_version
                    ? `${r.old_version} → ${r.new_version}`
                    : (MCP_STATUS_LABEL[r.status] || r.status)}
                </span>
                <span className="text-p-text-light whitespace-nowrap">{relativeTime(r.ts)}</span>
              </div>
            ))}
          </div>
        </details>
      )}
    </div>
  )
}
// ---------------------------------------------------------------------------
// System Settings tab — operational platform settings (timezone, sessions,
// remote-machine policy, concurrency, storage, memory, MCP auto-update).
// ---------------------------------------------------------------------------

export default function SystemSettingsTab() {
  const { user } = useAuth()
  const { data, isLoading } = usePlatformSettings()
  const saveMutation = useSavePlatformSettings()
  // Hide the remote-machine policy rows when this build ships without the
  // feature (settings for an absent feature are dead controls).
  const remoteMachinesAvailable = user?.feature_flags?.remote_machines_available !== false
  const { data: stats } = useConcurrencyStats()
  const { data: memory } = useMemorySettings()
  const saveMemory = useUpdateMemorySettings()

  const [timezone, setTimezone] = useState('UTC')
  const [sessionTimeout, setSessionTimeout] = useState('3600')
  const [jwtExpiry, setJwtExpiry] = useState('168')
  const [savedField, setSavedField] = useState('')
  const [idleTimeout, setIdleTimeout] = useState('900')
  const [fallbackUserOverride, setFallbackUserOverride] = useState(true)
  const [fallbackAgentDefault, setFallbackAgentDefault] = useState(false)
  const [allowUserPaired, setAllowUserPaired] = useState(true)
  const [interactiveCli, setInteractiveCli] = useState(false)
  const [retentionEnabled, setRetentionEnabled] = useState(true)
  const [retentionDays, setRetentionDays] = useState('180')
  const [mcpAutoUpdate, setMcpAutoUpdate] = useState(true)
  const [quotas, setQuotas] = useState<Record<string, string>>({
    quota_shared_folder_mb: '15360',
    quota_user_folder_mb: '2048',
    quota_shared_folder_inodes: '0',
    quota_user_folder_inodes: '0',
  })

  useEffect(() => {
    if (data) {
      setTimezone(data.platform_timezone || 'UTC')
      setSessionTimeout(data.session_timeout || '3600')
      setJwtExpiry(data.jwt_expiry_hours || '168')
      setIdleTimeout(data.session_idle_timeout || '900')
      setFallbackUserOverride(data.remote_fallback_user_override !== false)
      setFallbackAgentDefault(data.remote_fallback_agent_default === true)
      setAllowUserPaired(data.allow_user_paired_machines !== false)
      setInteractiveCli(data.interactive_cli_enabled === true)
      setRetentionEnabled(data.session_retention_enabled !== false)
      setRetentionDays(data.session_retention_days || '180')
      setMcpAutoUpdate(data.mcp_auto_update_enabled !== false)
      setQuotas({
        quota_shared_folder_mb: data.quota_shared_folder_mb || '15360',
        quota_user_folder_mb: data.quota_user_folder_mb || '2048',
        quota_shared_folder_inodes: data.quota_shared_folder_inodes || '0',
        quota_user_folder_inodes: data.quota_user_folder_inodes || '0',
      })
    }
  }, [data])

  const save = (field: string, value: string | boolean) => {
    saveMutation.mutate(
      { [field]: value },
      {
        onSuccess: () => {
          setSavedField(field)
          setTimeout(() => setSavedField(''), 2000)
        },
      },
    )
  }
  const isForced = (k: string) => (data?.forced_keys || []).includes(k)

  const onQuotaChange = (k: string, v: string) => setQuotas((prev) => ({ ...prev, [k]: v }))
  const onQuotaSave = (k: string) => {
    if (quotas[k] !== (data as unknown as Record<string, string>)?.[k]) save(k, quotas[k])
  }

  if (isLoading) return <p className="text-sm text-p-text-secondary">Loading...</p>

  return (
    <div className="space-y-6">
      {/* System Settings */}
      <div className="bg-white dark:bg-p-surface rounded-xl border border-p-border-light p-5 space-y-5">
        <h3 className="text-sm font-semibold text-p-text">System Settings</h3>

        {/* Timezone */}
        <div className="flex items-center justify-between gap-4">
          <div className="flex-1">
            <label className="block text-sm font-medium text-p-text mb-0.5">Platform Timezone</label>
            <p className="text-xs text-p-text-light">
              Used for task scheduling, cron jobs, and datetime injection in agent messages.
            </p>
          </div>
          <div className="flex items-center gap-2">
            <select
              value={timezone}
              onChange={(e) => { setTimezone(e.target.value); save('platform_timezone', e.target.value) }}
              className="px-2.5 py-1.5 text-sm border border-p-border-light rounded-lg bg-p-bg text-p-text focus:outline-hidden focus:ring-2 focus:ring-brand/30"
            >
              {TIMEZONE_OPTIONS.map(tz => (
                <option key={tz} value={tz}>{tz}</option>
              ))}
            </select>
            <SavedBadge show={savedField === 'platform_timezone'} />
          </div>
        </div>

        {/* Session Timeout */}
        <div className="flex items-center justify-between gap-4">
          <div className="flex-1">
            <label className="block text-sm font-medium text-p-text mb-0.5">Session Timeout</label>
            <p className="text-xs text-p-text-light">
              Maximum duration (seconds) for a single CLI session before it times out.
            </p>
          </div>
          <div className="flex items-center gap-2">
            <input
              type="number"
              value={sessionTimeout}
              onChange={(e) => setSessionTimeout(e.target.value)}
              onBlur={() => { if (sessionTimeout !== data?.session_timeout) save('session_timeout', sessionTimeout) }}
              min={60}
              className="w-24 px-2.5 py-1.5 text-sm border border-p-border-light rounded-lg bg-p-bg text-p-text focus:outline-hidden focus:ring-2 focus:ring-brand/30 text-right"
            />
            <SavedBadge show={savedField === 'session_timeout'} />
          </div>
        </div>

        {/* JWT Expiry */}
        <div className="flex items-center justify-between gap-4">
          <div className="flex-1">
            <label className="block text-sm font-medium text-p-text mb-0.5">Login Session Duration</label>
            <p className="text-xs text-p-text-light">
              Max hours of inactivity before a user must sign in again. Active sessions refresh
              automatically (sliding window), so this only logs out idle sessions.
            </p>
          </div>
          <div className="flex items-center gap-2">
            <input
              type="number"
              value={jwtExpiry}
              onChange={(e) => setJwtExpiry(e.target.value)}
              onBlur={() => { if (jwtExpiry !== data?.jwt_expiry_hours) save('jwt_expiry_hours', jwtExpiry) }}
              min={1}
              className="w-24 px-2.5 py-1.5 text-sm border border-p-border-light rounded-lg bg-p-bg text-p-text focus:outline-hidden focus:ring-2 focus:ring-brand/30 text-right"
            />
            <span className="text-xs text-p-text-light">hours</span>
            <SavedBadge show={savedField === 'jwt_expiry_hours'} />
          </div>
        </div>

        {/* Platform Public URL is set via DASHBOARD_PUBLIC_URL (config.env) —
            the admin field was removed; deployments configure it in compose. */}

        {/* Allow users to pair their own remote machines (kill-switch) */}
        {remoteMachinesAvailable && (<>
        <div className="flex items-center justify-between gap-4">
          <div className="flex-1">
            <label className="block text-sm font-medium text-p-text mb-0.5">Allow users to pair their own remote machines</label>
            <p className="text-xs text-p-text-light">
              When users pair a machine, they can run their own agents on their own hardware (laptops, home servers).
              <strong className="text-amber-700 dark:text-amber-400"> Turning this off immediately disconnects all user-paired satellites, deletes their agent assignments, and prevents new pairings.</strong>
              {' '}Admin-paired machines are unaffected.
            </p>
          </div>
          <div className="flex items-center gap-2">
            <input
              type="checkbox"
              checked={allowUserPaired}
              onChange={(e) => {
                const next = e.target.checked
                if (!next) {
                  // Confirm before disabling so admins don't nuke user-paired
                  // satellites accidentally.
                  if (!window.confirm(
                    "This will disconnect every user-paired satellite, " +
                    "delete every user_remote_targets row, and refuse new " +
                    "user pairings until re-enabled.\n\nContinue?"
                  )) return
                }
                setAllowUserPaired(next)
                save('allow_user_paired_machines', next)
              }}
              className="h-4 w-4 text-brand rounded-sm focus:ring-2 focus:ring-brand/30"
            />
            <SavedBadge show={savedField === 'allow_user_paired_machines'} />
          </div>
        </div>

        {/* Remote fallback: user override offline */}
        <div className="flex items-center justify-between gap-4">
          <div className="flex-1">
            <label className="block text-sm font-medium text-p-text mb-0.5">Fall back to local when a user's machine is offline</label>
            <p className="text-xs text-p-text-light">
              When a user has set a personal remote target that's unreachable, the session runs on the platform instead of hard-failing.
            </p>
          </div>
          <div className="flex items-center gap-2">
            <input
              type="checkbox"
              checked={fallbackUserOverride}
              onChange={(e) => { setFallbackUserOverride(e.target.checked); save('remote_fallback_user_override', e.target.checked) }}
              className="h-4 w-4 text-brand rounded-sm focus:ring-2 focus:ring-brand/30"
            />
            <SavedBadge show={savedField === 'remote_fallback_user_override'} />
          </div>
        </div>

        {/* Remote fallback: agent default offline */}
        <div className="flex items-center justify-between gap-4">
          <div className="flex-1">
            <label className="block text-sm font-medium text-p-text mb-0.5">Fall back to local when the agent's remote target is offline</label>
            <p className="text-xs text-p-text-light">
              When an admin-configured remote target is unreachable, fall back to the platform. Off by default — admin assignments often imply data-residency intent.
            </p>
          </div>
          <div className="flex items-center gap-2">
            <input
              type="checkbox"
              checked={fallbackAgentDefault}
              onChange={(e) => { setFallbackAgentDefault(e.target.checked); save('remote_fallback_agent_default', e.target.checked) }}
              className="h-4 w-4 text-brand rounded-sm focus:ring-2 focus:ring-brand/30"
            />
            <SavedBadge show={savedField === 'remote_fallback_agent_default'} />
          </div>
        </div>
        </>)}

        {/* Interactive terminal sessions (global kill-switch, default OFF) */}
        <div className="flex items-center justify-between gap-4">
          <div className="flex-1">
            <label className="block text-sm font-medium text-p-text mb-0.5">Interactive terminal sessions</label>
            <p className="text-xs text-p-text-light">
              Let chats and tasks run the native Claude Code / Codex TUI as a live terminal in the
              dashboard, instead of the standard headless stream. Off = every session runs headless
              and the interactive toggles are hidden.
            </p>
          </div>
          <div className="flex items-center gap-2">
            <input
              type="checkbox"
              checked={interactiveCli}
              onChange={(e) => { setInteractiveCli(e.target.checked); save('interactive_cli_enabled', e.target.checked) }}
              className="h-4 w-4 text-brand rounded-sm focus:ring-2 focus:ring-brand/30"
            />
            <SavedBadge show={savedField === 'interactive_cli_enabled'} />
          </div>
        </div>
      </div>

      {/* Concurrency */}
      <div className="bg-white dark:bg-p-surface rounded-xl border border-p-border-light p-5 space-y-5">
        <div>
          <h3 className="text-sm font-semibold text-p-text">Concurrency</h3>
          <p className="text-xs text-p-text-light mt-0.5">
            Fully automatic — local agent sessions (chat, phone, tasks, meetings) are admitted live
            against this server's free memory. Under load, the oldest idle sessions are gracefully
            reclaimed before any are refused. No tuning needed.
          </p>
        </div>

        {/* Read-only live memory gauge — admission is automatic, no knobs. */}
        <div className="space-y-2">
          <div className="text-sm font-medium text-p-text">
            {stats?.sessions?.active ?? 0} active local session{(stats?.sessions?.active ?? 0) === 1 ? '' : 's'}
          </div>
          {stats?.sessions && (
            <>
              <p className="text-xs text-p-text-light">
                Room for about <strong>{stats.sessions.fit_heavy}</strong> more heavy or{' '}
                <strong>{stats.sessions.fit_light}</strong> light session{stats.sessions.fit_light === 1 ? '' : 's'} right now.
              </p>
              <div className="h-2 rounded-full bg-p-bg overflow-hidden" title="Reserved vs budget">
                <div
                  className="h-full bg-brand"
                  style={{ width: `${Math.min(100, Math.round(100 * stats.sessions.reserved_mb / Math.max(1, stats.sessions.budget_mb)))}%` }}
                />
              </div>
              <p className="text-[11px] text-p-text-light/80 font-mono">
                {(stats.sessions.reserved_mb / 1024).toFixed(1)} / {(stats.sessions.budget_mb / 1024).toFixed(1)} GB reserved
                {' · '}{(stats.sessions.available_mb / 1024).toFixed(1)} GB free of {(stats.sessions.total_mb / 1024).toFixed(1)} GB
              </p>
            </>
          )}
          {stats?.by_surface && (
            <p className="text-[11px] text-p-text-light/80 font-mono">
              chat {stats.by_surface.chat} · task {stats.by_surface.task} · meeting {stats.by_surface.meeting} · phone {stats.by_surface.phone}
            </p>
          )}
        </div>

        {/* The one remaining knob: the shared idle-reap timeout. */}
        <ConcurrencyRow
          label="Idle timeout (seconds)"
          description="Reap an inactive session after this long — chat and task sessions. Under memory pressure, idle sessions may be reclaimed sooner."
          value={idleTimeout}
          min={60}
          forced={isForced('session_idle_timeout')}
          onChange={setIdleTimeout}
          onSave={() => { if (idleTimeout !== (data?.session_idle_timeout || '')) save('session_idle_timeout', idleTimeout) }}
          savedField={savedField}
          fieldKey="session_idle_timeout"
        />
      </div>

      {/* Memory — capture & injection tuning. The platform-wide enable lives in
          code (memory is always on); users clear their own memory in their
          settings and managers clear an agent's memory in agent settings. */}
      {memory && (
        <div className="bg-white dark:bg-p-surface rounded-xl border border-p-border-light p-5 space-y-4">
          <div>
            <h3 className="text-sm font-semibold text-p-text">Memory</h3>
            <p className="text-xs text-p-text-light mt-0.5">
              Agents maintain memory inline with the <code>memory</code> tool; content reaches
              their system prompt automatically.
            </p>
          </div>
          <div className="grid grid-cols-1 sm:grid-cols-2 gap-3">
            <label className="block">
              <span className="block text-xs text-p-text-secondary mb-1">Inline budget (bytes)</span>
              <input type="number" min={1024} step={1024}
                value={memory.inline_budget_bytes}
                onChange={(e) => saveMemory.mutate({ inline_budget_bytes: parseInt(e.target.value || '8192', 10) })}
                className="w-full text-sm px-2 py-1.5 border border-p-border-light rounded-sm bg-p-bg text-p-text"
              />
              <span className="block text-[10px] text-p-text-light mt-1">
                Per scope: topic files inject in full into the prompt under this size; past it
                only the generated index is injected.
              </span>
            </label>
            <label className="block">
              <span className="block text-xs text-p-text-secondary mb-1">Capture nudge (turns)</span>
              <input type="number" min={0}
                value={memory.nudge_turns}
                onChange={(e) => saveMemory.mutate({ nudge_turns: parseInt(e.target.value || '0', 10) })}
                className="w-full text-sm px-2 py-1.5 border border-p-border-light rounded-sm bg-p-bg text-p-text"
              />
              <span className="block text-[10px] text-p-text-light mt-1">
                After this many chat turns without a memory save, a one-line reminder to capture
                memory rides the next message. 0 disables it.
              </span>
            </label>
          </div>
        </div>
      )}

      {/* Storage & Retention — hidden on the OtoDock cloud (operator owns disk
          policy: retention is forced ON at the default window server-side). */}
      {!data?.cloud && (
        <StorageRetentionCard
          enabled={retentionEnabled}
          days={retentionDays}
          onEnabledChange={(v) => { setRetentionEnabled(v); save('session_retention_enabled', v) }}
          onDaysChange={setRetentionDays}
          onSaveDays={() => { if (retentionDays !== data?.session_retention_days) save('session_retention_days', retentionDays) }}
          savedField={savedField}
        />
      )}

      {/* Storage Quotas — per-agent disk limits on local agent folders (hidden
          on cloud, where the operator pins them via OTODOCK_FORCED_SETTINGS). */}
      {!data?.cloud && (
        <StorageQuotasCard
          values={quotas}
          enforced={data?.storage_quotas_enforced === true}
          forcedKeys={data?.forced_keys || []}
          onChange={onQuotaChange}
          onSave={onQuotaSave}
          savedField={savedField}
        />
      )}

      {/* Chat Title Generation — provider/model from the Direct LLM execution
          layer (replaces the old OpenAI title key). */}
      <ChatTitleGenerationCard />

      {/* Automatic MCP updates — weekly community-MCP update job. Shown on every
          tier (on cloud, docker MCPs are managed centrally and skipped). */}
      <McpAutoUpdateCard
        enabled={mcpAutoUpdate}
        forced={isForced('mcp_auto_update_enabled')}
        saved={savedField === 'mcp_auto_update_enabled'}
        onToggle={(v) => { setMcpAutoUpdate(v); save('mcp_auto_update_enabled', v) }}
      />
    </div>
  )
}
