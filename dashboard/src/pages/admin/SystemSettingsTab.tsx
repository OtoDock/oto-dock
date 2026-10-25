import { useLayoutEffect, useRef, useState } from 'react'
import { useQueryClient } from '@tanstack/react-query'
import { useFlag } from '../../hooks/useFlag'
import { useTitleGeneration, useSaveTitleGeneration, useCheckTitleGeneration } from '../../api/titleGeneration'
import { useMcpAutoUpdateLog, type McpAutoUpdateRow } from '../../api/mcps'
import { useMemorySettings, useUpdateMemorySettings } from '../../api/memory'
import {
  usePlatformSettings,
  useSavePlatformSettings,
  useConcurrencyStats,
  useStorageUsage,
} from './PlatformPage.hooks'
import { ConcurrencyRow, QuotaRow } from './PlatformPage.shared'
import type { PlatformSettings } from './PlatformPage.types'
import { SaveState, SettingRow } from '../../components/ui/SettingsControls'
import { useSaveState, type SaveStatus } from '../../hooks/useSaveState'
import InfoTip from '../../components/ui/InfoTip'
import OffboardedArchivesCard from './OffboardedArchivesCard'
import { formatBytes, relativeTime, TIMEZONE_OPTIONS, MCP_STATUS_LABEL } from './PlatformPage.format'
import { apiJson, withMessage } from '../../lib/http/apiJson'
import { useDialogs } from '../../components/ui/dialogs'
import Button from '../../components/ui/Button'
import { Input, Select } from '../../components/ui/Field'
import { BADGE_BASE, BADGE_TONE, CARD } from '../../components/ui/classes'

// ---------------------------------------------------------------------------
// Storage & Retention card (Setup → System Settings). Settings ride the shared
// platform-settings PUT; usage + run-now hit api/admin/admin_storage.py.
// ---------------------------------------------------------------------------

function StorageRetentionCard({
  enabled, days, onEnabledChange, onDaysChange, onSaveDays, stateOf,
}: {
  enabled: boolean
  days: string
  onEnabledChange: (v: boolean) => void
  onDaysChange: (v: string) => void
  onSaveDays: () => void
  stateOf: (key: string) => SaveStatus
}) {
  const qc = useQueryClient()
  const { data: usage } = useStorageUsage()
  const [running, setRunning] = useState(false)
  const [result, setResult] = useState<string | null>(null)
  const [resultOk, setResultOk] = useState(true)

  const runNow = async (dryRun: boolean) => {
    setRunning(true); setResult(null)
    try {
      const d = await apiJson<RetentionRun>('/v1/admin/retention/run-now', {
        method: 'POST', body: JSON.stringify({ dry_run: dryRun }),
      }).catch(withMessage('Request failed'))
      const freed = (d.bytes_freed || 0) + (d.orphan_bytes || 0)
        + (d.codex_junk_bytes || 0) + (d.tarball_bytes || 0) + (d.offboarded_bytes || 0)
        + (d.remote_bytes_freed || 0)
      const reached = (d.remote_machines ?? []).filter((m) => m.reached).length
      setResultOk(true)
      setResult(
        `${dryRun ? 'Would free' : 'Freed'} ${formatBytes(freed)} · `
        + `${d.chats_flagged ?? 0} chats aged out · ${d.orphans_deleted ?? 0} orphans · `
        + `${d.codex_junk_files ?? 0} junk files`
        + `${reached ? ` · ${d.remote_session_files_deleted ?? 0} session files on ${reached} machine${reached === 1 ? '' : 's'}` : ''}`
        + `${d.offboarded_deleted ? ` · ${d.offboarded_deleted} archives of removed people` : ''}`
        + `${d.errors ? ` · ${d.errors} errors` : ''}`,
      )
      if (!dryRun) {
        qc.invalidateQueries({ queryKey: ['storage-usage'] })
        qc.invalidateQueries({ queryKey: ['offboarded-archives'] })
      }
    } catch {
      setResultOk(false)
      setResult('The cleanup did not run. Try again.')
    } finally {
      setRunning(false)
    }
  }

  const last = usage?.retention?.last_sweep
  // A null is a value nobody measured yet (the machines' files before the
  // first sweep reached one): shown as a dash, never as zero.
  const usageRows: Array<[string, number | null | undefined]> = [
    ['Agents folder, total', usage?.agents_bytes],
    ['Session files', usage?.session_files_bytes],
    ['Session files on machines', usage?.remote_session_files_bytes],
    ['Codex junk', usage?.codex_junk_bytes],
    ['Recover bin', usage?.recover_bin_bytes],
    ['Server cache', usage?.sessions_dir_bytes],
    ['Logs', usage?.logs_bytes],
  ]

  return (
    <div className={`${CARD} p-5 space-y-5`}>
      <h3 className="text-sm font-semibold text-primary">Storage and Retention</h3>

      <SettingRow save={stateOf('session_retention_enabled')}
        label="Clean up old chat session files"
        htmlFor="session-retention-enabled"
        description="Chats left untouched for the period below lose their session files."
        info={<>
          The cleanup runs on this server and on the machines the chats ran on. Their full history
          stays saved. Reopening one starts a fresh session from that history.
        </>}
      >
        <input
          id="session-retention-enabled"
          type="checkbox"
          checked={enabled}
          onChange={(e) => onEnabledChange(e.target.checked)}
          className="h-4 w-4 accent-action"
        />
      </SettingRow>

      <SettingRow save={stateOf('session_retention_days')}
        label="Days to keep session files"
        htmlFor="session-retention-days"
        description="Minimum 7."
        info="Sessions idle longer than this are removed in the daily cleanup."
      >
        <Input
          id="session-retention-days"
          type="number"
          value={days}
          onChange={(e) => onDaysChange(e.target.value)}
          onBlur={onSaveDays}
          min={7}
          disabled={!enabled}
          className="max-w-20 text-right"
        />
      </SettingRow>

      <div>
        <p className="text-sm font-medium text-primary mb-2">Disk usage</p>
        <div className="grid grid-cols-2 sm:grid-cols-3 gap-x-6 gap-y-1">
          {usageRows.map(([label, bytes]) => (
            <div key={label} className="flex items-center justify-between gap-2 text-xs">
              <span className="text-tertiary truncate">{label}</span>
              <span className="text-primary tabular-nums shrink-0">{usage ? (bytes === null ? '—' : formatBytes(bytes)) : '…'}</span>
            </div>
          ))}
        </div>
        {last && (
          <p className="text-xs text-tertiary mt-2">
            Last cleanup: {new Date(String(last.ran_at)).toLocaleString()} ·
            freed {formatBytes(
              (Number(last.bytes_freed) || 0) + (Number(last.orphan_bytes) || 0)
              + (Number(last.codex_junk_bytes) || 0) + (Number(last.tarball_bytes) || 0)
              + (Number(last.offboarded_bytes) || 0) + (Number(last.remote_bytes_freed) || 0),
            )}
            {(Number(last.recover_bin_rows_pruned) || Number(last.recover_bin_orphans_deleted))
              ? ` · ${(Number(last.recover_bin_rows_pruned) || 0) + (Number(last.recover_bin_orphans_deleted) || 0)} old recover bin entries removed`
              : ''}
          </p>
        )}
      </div>

      <div className="flex items-center gap-3 flex-wrap">
        <Button variant="primary" size="xs"
          onClick={() => runNow(false)}
          disabled={running}
        >
          {running ? 'Running…' : 'Run cleanup now'}
        </Button>
        <Button variant="secondary" size="xs"
          onClick={() => runNow(true)}
          disabled={running}
        >
          Dry run
        </Button>
        {result && (
          <span className={`text-xs font-medium ${resultOk ? 'text-status-success-fg' : 'text-status-danger-fg'}`}>
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
  values, enforced, forcedKeys, onChange, onSave, stateOf,
}: {
  values: Record<string, string>
  enforced: boolean
  forcedKeys: string[]
  onChange: (k: string, v: string) => void
  onSave: (k: string) => void
  stateOf: (key: string) => SaveStatus
}) {
  const isForced = (k: string) => forcedKeys?.includes(k)
  const gbHint = (mb: string) => {
    const n = Number(mb)
    if (!Number.isFinite(n) || n <= 0) return 'Unlimited'
    return n % 1024 === 0 ? `${n / 1024} GB` : `${(n / 1024).toFixed(1)} GB`
  }
  const inodeHint = (v: string) => (Number(v) > 0 ? `${Number(v).toLocaleString()} files` : 'Unlimited')

  return (
    <div className={`${CARD} p-5 space-y-5`}>
      <div className="flex items-center justify-between gap-3">
        <h3 className="text-sm font-semibold text-primary">Storage Quotas</h3>
        <span className={`${BADGE_BASE} ${BADGE_TONE[enforced ? 'success' : 'neutral']} shrink-0`}>
          {enforced ? 'Hard enforcement on XFS' : 'Measurement and warnings'}
        </span>
      </div>

      <p className="text-xs text-tertiary">
        Disk limits for each agent's folders on this server. A warning goes out at 90, 95
        and 100% full. The owner gets it for a personal folder. Managers and editors get it
        for a shared folder. Set a limit to <span className="font-medium">0</span> for unlimited.{' '}
        {enforced
          ? 'Hard limits are on. Nothing can be written past a limit. Remote machines follow the same limits. Files copied back into a full folder fail and retry later.'
          : 'Measurement and warnings only. Hard limits need the agents data folder on an XFS volume with project quota. They then turn on by themselves.'}
      </p>

      <QuotaRow
        label="Shared agent folder"
        desc="Workspace, knowledge and settings together"
        hint={gbHint(values.quota_shared_folder_mb)}
        unit="MB"
        value={values.quota_shared_folder_mb}
        forced={isForced('quota_shared_folder_mb')}
        onChange={(v) => onChange('quota_shared_folder_mb', v)}
        onSave={() => onSave('quota_shared_folder_mb')}
        save={stateOf('quota_shared_folder_mb')}
      />
      <QuotaRow
        label="Per-user folder"
        desc="each user's personal users/{name}/ folder"
        hint={gbHint(values.quota_user_folder_mb)}
        unit="MB"
        value={values.quota_user_folder_mb}
        forced={isForced('quota_user_folder_mb')}
        onChange={(v) => onChange('quota_user_folder_mb', v)}
        onSave={() => onSave('quota_user_folder_mb')}
        save={stateOf('quota_user_folder_mb')}
      />

      <details className="group">
        <summary className="text-xs font-medium text-secondary cursor-pointer select-none hover:text-primary">
          File-count limits, advanced and off by default
        </summary>
        <div className="mt-3 space-y-4">
          <QuotaRow
            label="Shared folder file limit"
            desc="Max files in the shared agent folder"
            hint={inodeHint(values.quota_shared_folder_inodes)}
            unit="files"
            value={values.quota_shared_folder_inodes}
            forced={isForced('quota_shared_folder_inodes')}
            onChange={(v) => onChange('quota_shared_folder_inodes', v)}
            onSave={() => onSave('quota_shared_folder_inodes')}
            save={stateOf('quota_shared_folder_inodes')}
          />
          <QuotaRow
            label="Per-user folder file limit"
            desc="Max files in each user's personal folder"
            hint={inodeHint(values.quota_user_folder_inodes)}
            unit="files"
            value={values.quota_user_folder_inodes}
            forced={isForced('quota_user_folder_inodes')}
            onChange={(v) => onChange('quota_user_folder_inodes', v)}
            onSave={() => onSave('quota_user_folder_inodes')}
            save={stateOf('quota_user_folder_inodes')}
          />
        </div>
      </details>
    </div>
  )
}
// Chat-title generation — provider/model sourced from the Direct LLM execution
// layer (replaces the old per-key OpenAI title path). Toggle + model dropdown +
// Active/Inactive badge, mirroring the phone Turn-Classifier card.
/** The retention sweep's counts (a dry run counts what it would remove). */
interface RetentionRun {
  bytes_freed?: number
  orphan_bytes?: number
  codex_junk_bytes?: number
  tarball_bytes?: number
  offboarded_bytes?: number
  remote_bytes_freed?: number
  chats_flagged?: number
  orphans_deleted?: number
  codex_junk_files?: number
  offboarded_deleted?: number
  remote_session_files_deleted?: number
  /** One row per machine asked; `reached` is false when it did not answer. */
  remote_machines?: Array<{
    machine_id: string
    name: string
    files: number
    bytes: number
    deleted: number
    deleted_bytes: number
    reached: boolean
  }>
  errors?: number
}

export function ChatTitleGenerationCard() {
  const { data: tg, isLoading } = useTitleGeneration()
  const saveMutation = useSaveTitleGeneration()
  const check = useCheckTitleGeneration()
  const { stateOf, run } = useSaveState()

  if (isLoading || !tg) {
    return (
      <div className={`${CARD} p-5`}>
        <p className="text-sm text-secondary">Loading...</p>
      </div>
    )
  }

  const autoLabel = (!tg.selected_model && tg.active_model)
    ? `Auto: ${tg.active_model}`
    : 'Auto: first configured provider'
  // Configured is not working: the last title (or Check) says which it is.
  const failing = tg.active && tg.last_attempt?.ok === false
  const badge = !tg.active ? 'Inactive' : failing ? 'Not working' : 'Active'

  return (
    <div className={`${CARD} p-5 space-y-4`}>
      <div className="flex items-center justify-between">
        <h3 className="text-sm font-semibold text-primary">Chat Title Generation</h3>
        <span className={`${BADGE_BASE} ${BADGE_TONE[failing ? 'warning' : tg.active ? 'success' : 'neutral']}`}>
          {badge}
        </span>
      </div>
      <p className="text-xs text-tertiary">
        Gives a new dashboard chat a short title with an emoji. It sums up the first
        message and reply. The provider and model come from the Direct LLM AI engine.
        No separate API key is needed.
      </p>

      {/* Enable toggle */}
      <SettingRow
        label="Enabled"
        htmlFor="title-gen-enabled"
        info="When off, titles stay as the first words of the prompt."
        save={stateOf('enabled')}
      >
        <input
          id="title-gen-enabled"
          type="checkbox"
          checked={tg.enabled}
          onChange={(e) => {
            const enabled = e.target.checked
            void run('enabled', () => saveMutation.mutateAsync({ enabled }), { failTitle: 'The title setting was not saved' })
          }}
          disabled={saveMutation.isPending}
          className="h-4 w-4 accent-action"
        />
      </SettingRow>

      {/* Model — flex-wrap + w-full on mobile: the long "label — model" option
          text otherwise forces the select past the page width */}
      <div className="flex flex-wrap items-center justify-between gap-4">
        <div className="flex-1">
          <p className="text-sm font-medium text-primary mb-0.5"><label>Model</label><SaveState state={stateOf('model')} /></p>
        </div>
        <Select
          value={tg.selected_model}
          onChange={(e) => {
            const model = e.target.value
            void run('model', () => saveMutation.mutateAsync({ model }), { failTitle: 'The title model was not saved' })
          }}
          disabled={!tg.enabled || saveMutation.isPending}
          className="sm:w-auto min-w-0 max-w-full"
        >
          <option value="">{autoLabel}</option>
          {tg.options.map((o) => (
            <option key={o.model} value={o.model}>{o.label} ({o.model})</option>
          ))}
        </Select>
      </div>

      {tg.unlisted_pin && (
        <p className="text-xs text-status-warning-fg" data-testid="title-gen-unlisted">
          The pinned model {tg.unlisted_pin} is no longer offered. Titles use Auto.
        </p>
      )}

      {!tg.active && tg.enabled && (
        <p className="text-xs text-status-warning-fg">
          No Direct LLM provider is set up. Add one in Setup, AI Engines, Direct LLM to turn this on.
        </p>
      )}

      {tg.active && (
        <div className="flex flex-wrap items-center justify-between gap-2">
          <p className="min-w-0 flex-1 text-xs text-tertiary" data-testid="title-gen-last">
            {failing
              ? <span className="text-status-warning-fg">Titles are not being made: {tg.last_attempt?.reason}.</span>
              : tg.last_attempt?.ok
                ? tg.last_attempt.kind === 'credential'
                  ? 'The credential resolves. No title made since the server started.'
                  : 'The last title came through.'
                : 'No title tried since the server started. Check makes sure the credential resolves.'}
          </p>
          <Button variant="secondary" size="xs"
            type="button"
            onClick={() => check.mutate()}
            disabled={check.isPending}
            className="shrink-0"
          >
            {check.isPending ? 'Checking…' : 'Check'}
          </Button>
        </div>
      )}
      {check.isError && (
        <p className="text-xs text-status-danger-fg" role="alert">{(check.error as Error).message}</p>
      )}
    </div>
  )
}
// Automatic MCP updates — toggle + quiet status line + inline run history.
// The toggle is part of platform-settings (saved by this tab); the history
// comes from its own endpoint.
function McpAutoUpdateCard({ enabled, forced, save, onToggle }: {
  enabled: boolean
  forced: boolean
  save: SaveStatus
  onToggle: (v: boolean) => void
}) {
  const { data } = useMcpAutoUpdateLog()
  const runs = data?.runs || []
  const lastRunAt = data?.last_run_at || ''

  // Counts for the most recent run (rows arrive newest-first).
  const latestRunId = runs[0]?.run_id
  const latest = latestRunId ? runs.filter((r) => r.run_id === latestRunId) : []
  const count = (s: string) => latest.filter((r) => r.status === s).length

  let statusLine = 'Has not run yet. The first run happens in the next weekly window.'
  if (lastRunAt) {
    if (latest.length) {
      const parts = [
        `${count('updated')} updated`,
        count('failed') ? `${count('failed')} failed` : null,
        count('skipped_in_use') ? `${count('skipped_in_use')} skipped while in use` : null,
        count('held') ? `${count('held')} held` : null,
        count('needs_approval') ? `${count('needs_approval')} with a new source to approve` : null,
      ].filter(Boolean)
      statusLine = `Last run ${relativeTime(lastRunAt)}: ${parts.join(', ')}.`
    } else {
      statusLine = `Last run ${relativeTime(lastRunAt)}: everything was up to date.`
    }
  }

  return (
    <div className={`${CARD} p-5 space-y-4`}>
      <div>
        <h3 className="inline text-sm font-semibold text-primary">Automatic MCP Updates</h3>
        <InfoTip label="Automatic MCP Updates">
          Once a week, in a low-traffic window, OtoDock applies available updates to community
          MCPs. Docker MCPs in use wait until they are free. Only failures notify admins.
        </InfoTip>
      </div>

      <SettingRow label="Enabled" htmlFor="mcp-auto-update-enabled" description={statusLine} save={save}>
        <input
          id="mcp-auto-update-enabled"
          type="checkbox"
          checked={enabled}
          disabled={forced}
          onChange={(e) => onToggle(e.target.checked)}
          className="h-4 w-4 accent-action disabled:opacity-50 disabled:cursor-not-allowed"
        />
      </SettingRow>

      {runs.length > 0 && (
        <details className="group">
          <summary className="text-xs font-medium text-secondary cursor-pointer select-none hover:text-primary">
            Recent updates ({runs.length})
          </summary>
          <div className="mt-3 space-y-1.5">
            {runs.map((r: McpAutoUpdateRow, i: number) => (
              <div key={i} className="flex items-center justify-between gap-3 text-xs">
                <span className="text-primary font-medium truncate">{r.mcp_name}</span>
                <span className={`whitespace-nowrap ${r.status === 'failed' ? 'text-status-danger-fg' : 'text-tertiary'}`}>
                  {r.status === 'updated' && r.old_version && r.new_version
                    ? `${r.old_version} → ${r.new_version}`
                    : (MCP_STATUS_LABEL[r.status] || r.status)}
                </span>
                <span className="text-tertiary whitespace-nowrap">{relativeTime(r.ts)}</span>
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
  const dialogs = useDialogs()
  const { data, isLoading } = usePlatformSettings()
  const saveMutation = useSavePlatformSettings()
  // Hide the remote-machine policy rows when this build ships without the
  // feature (settings for an absent feature are dead controls).
  const remoteMachinesAvailable = useFlag('remote_machines_available')
  const { data: stats } = useConcurrencyStats()
  const { data: memory } = useMemorySettings()
  const saveMemory = useUpdateMemorySettings()

  const [timezone, setTimezone] = useState('UTC')
  const [sessionTimeout, setSessionTimeout] = useState('3600')
  const [jwtExpiry, setJwtExpiry] = useState('168')
  const { stateOf, run } = useSaveState()
  const [idleTimeout, setIdleTimeout] = useState('900')
  const [fallbackUserOverride, setFallbackUserOverride] = useState(true)
  const [fallbackAgentDefault, setFallbackAgentDefault] = useState(false)
  const [allowUserPaired, setAllowUserPaired] = useState(true)
  const [interactiveCli, setInteractiveCli] = useState(false)
  const [retentionEnabled, setRetentionEnabled] = useState(true)
  const [retentionDays, setRetentionDays] = useState('180')
  const [archiveEnabled, setArchiveEnabled] = useState(true)
  const [archiveDays, setArchiveDays] = useState('180')
  const [mcpAutoUpdate, setMcpAutoUpdate] = useState(true)
  const [windowsEnabled, setWindowsEnabled] = useState(true)
  const [quotas, setQuotas] = useState<Record<string, string>>({
    quota_shared_folder_mb: '15360',
    quota_user_folder_mb: '2048',
    quota_shared_folder_inodes: '0',
    quota_user_folder_inodes: '0',
  })

  // The saved settings fill the form when they arrive or change (in render).
  const [filledFrom, setFilledFrom] = useState(data)
  if (data && filledFrom !== data) {
    setFilledFrom(data)
    {
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
      setArchiveEnabled(data.offboarded_retention_enabled !== false)
      setArchiveDays(data.offboarded_retention_days || '180')
      setMcpAutoUpdate(data.mcp_auto_update_enabled !== false)
      setWindowsEnabled(data.subscription_windows_enabled !== false)
      setQuotas({
        quota_shared_folder_mb: data.quota_shared_folder_mb || '15360',
        quota_user_folder_mb: data.quota_user_folder_mb || '2048',
        quota_shared_folder_inodes: data.quota_shared_folder_inodes || '0',
        quota_user_folder_inodes: data.quota_user_folder_inodes || '0',
      })
    }
  }

  // `revert` puts a toggle or a select back to the server's value as it is
  // when the refusal lands (two quick changes may both be refused); a typed
  // number stays.
  const stored = useRef(data)
  useLayoutEffect(() => { stored.current = data })
  const back = (apply: (d: PlatformSettings) => void) => () => { if (stored.current) apply(stored.current) }
  const save = (field: string, value: string | boolean, revert?: () => void) => {
    void run(field, () => saveMutation.mutateAsync({ [field]: value }),
      { failTitle: 'The setting was not saved', revert })
  }
  const isForced = (k: string) => (data?.forced_keys || []).includes(k)
  // The archive days: a destructive setting the server refuses out of range,
  // so its answer is shown, not swallowed.
  const saveArchiveDays = () => {
    if (archiveDays === data?.offboarded_retention_days) return
    void run('offboarded_retention_days', () => saveMutation.mutateAsync({ offboarded_retention_days: archiveDays }),
      { failTitle: 'The days to keep were not saved' })
  }

  const onQuotaChange = (k: string, v: string) => setQuotas((prev) => ({ ...prev, [k]: v }))
  const onQuotaSave = (k: string) => {
    if (quotas[k] !== (data as unknown as Record<string, string>)?.[k]) save(k, quotas[k])
  }

  if (isLoading) return <p className="text-sm text-secondary">Loading...</p>

  return (
    <div className="space-y-6">
      {/* System Settings */}
      <div className={`${CARD} p-5 space-y-5`}>
        <h3 className="text-sm font-semibold text-primary">System Settings</h3>

        {/* Timezone */}
        <SettingRow save={stateOf('platform_timezone')}
          label="Platform Timezone"
          htmlFor="platform-timezone"
          info="Used for scheduled tasks and for the date and time agents see in messages."
        >
          <Select
            id="platform-timezone"
            value={timezone}
            onChange={(e) => { setTimezone(e.target.value); save('platform_timezone', e.target.value, back((d) => setTimezone(d.platform_timezone || 'UTC'))) }}
          >
            {TIMEZONE_OPTIONS.map(tz => (
              <option key={tz} value={tz}>{tz}</option>
            ))}
          </Select>
        </SettingRow>

        {/* Session Timeout */}
        <div className="flex items-center justify-between gap-4">
          <div className="flex-1">
            <p className="text-sm font-medium text-primary mb-0.5"><label>Session Timeout</label><SaveState state={stateOf('session_timeout')} /></p>
            <p className="text-xs text-tertiary">
              The longest a single agent session may run, in seconds, before it times out.
            </p>
          </div>
          <div className="flex items-center gap-2">
            <Input
              type="number"
              value={sessionTimeout}
              onChange={(e) => setSessionTimeout(e.target.value)}
              onBlur={() => { if (sessionTimeout !== data?.session_timeout) save('session_timeout', sessionTimeout) }}
              min={60}
              className="max-w-24 text-right"
            />
          </div>
        </div>

        {/* JWT Expiry */}
        <SettingRow save={stateOf('jwt_expiry_hours')}
          label="Login Session Duration"
          htmlFor="jwt-expiry-hours"
          description="Active users stay signed in, so this only signs out idle sessions."
          info="Hours of inactivity before a user must sign in again."
        >
          <Input
            id="jwt-expiry-hours"
            type="number"
            value={jwtExpiry}
            onChange={(e) => setJwtExpiry(e.target.value)}
            onBlur={() => { if (jwtExpiry !== data?.jwt_expiry_hours) save('jwt_expiry_hours', jwtExpiry) }}
            min={1}
            className="max-w-24 text-right"
          />
          <span className="text-xs text-tertiary">hours</span>
        </SettingRow>

        {/* Platform Public URL is set via DASHBOARD_PUBLIC_URL (config.env) —
            the admin field was removed; deployments configure it in compose. */}

        {/* Allow users to pair their own remote machines (kill-switch) */}
        {remoteMachinesAvailable && (<>
        <SettingRow save={stateOf('allow_user_paired_machines')}
          label="Allow users to pair their own remote machines"
          htmlFor="allow-user-paired-machines"
          description={<>
            <strong className="text-status-warning-fg">Turning this off disconnects every user-paired machine at once. It also removes their agent assignments and blocks new pairings.</strong>
            {' '}Admin-paired machines are unaffected.
          </>}
          info="Users can pair their own machines, like laptops or home servers. Their agents can then run there."
        >
          <input
            id="allow-user-paired-machines"
            type="checkbox"
            checked={allowUserPaired}
            onChange={async (e) => {
              const next = e.target.checked
              if (!next) {
                // Confirm before disabling so admins don't nuke user-paired
                // satellites accidentally.
                if (!(await dialogs.confirm({
                  title: 'Turn off user-paired machines?',
                  body: 'This disconnects every user-paired machine. It removes every machine ' +
                    'choice people made for their agents. New user pairings are refused until ' +
                    'you turn it on again.',
                  confirmLabel: 'Turn off',
                  tone: 'danger',
                }))) return
              }
              setAllowUserPaired(next)
              save('allow_user_paired_machines', next, back((d) => setAllowUserPaired(d.allow_user_paired_machines !== false)))
            }}
            className="h-4 w-4 accent-action"
          />
        </SettingRow>

        {/* Remote fallback: user override offline */}
        <SettingRow save={stateOf('remote_fallback_user_override')}
          label="Fall back to local when a user's machine is offline"
          htmlFor="remote-fallback-user-override"
          info="If a user's chosen machine is offline, the session runs on the platform instead."
        >
          <input
            id="remote-fallback-user-override"
            type="checkbox"
            checked={fallbackUserOverride}
            onChange={(e) => { const on = e.target.checked; setFallbackUserOverride(on); save('remote_fallback_user_override', on, back((d) => setFallbackUserOverride(d.remote_fallback_user_override !== false))) }}
            className="h-4 w-4 accent-action"
          />
        </SettingRow>

        {/* Remote fallback: agent default offline */}
        <SettingRow save={stateOf('remote_fallback_agent_default')}
          label="Fall back to local when the agent's remote target is offline"
          htmlFor="remote-fallback-agent-default"
          description="Off by default, because an admin often picks a machine to keep the data there."
          info="When a machine an admin chose is offline, the session runs on the platform instead."
        >
          <input
            id="remote-fallback-agent-default"
            type="checkbox"
            checked={fallbackAgentDefault}
            onChange={(e) => { const on = e.target.checked; setFallbackAgentDefault(on); save('remote_fallback_agent_default', on, back((d) => setFallbackAgentDefault(d.remote_fallback_agent_default === true))) }}
            className="h-4 w-4 accent-action"
          />
        </SettingRow>
        </>)}

        {/* Interactive terminal sessions (global kill-switch, default ON since R1.5) */}
        <SettingRow save={stateOf('interactive_cli_enabled')}
          label="Interactive terminal sessions"
          htmlFor="interactive-cli-enabled"
          info={<>
            Chats and tasks can run Claude Code or Codex as a live dashboard terminal.
            Off: every session runs the standard way, and the interactive switches are hidden.
          </>}
        >
          <input
            id="interactive-cli-enabled"
            type="checkbox"
            checked={interactiveCli}
            onChange={(e) => { const on = e.target.checked; setInteractiveCli(on); save('interactive_cli_enabled', on, back((d) => setInteractiveCli(d.interactive_cli_enabled === true))) }}
            className="h-4 w-4 accent-action"
          />
        </SettingRow>

        {/* Provider windows (default ON): the pool reads each OAuth account's
            session and weekly usage from the vendor. */}
        <SettingRow save={stateOf('subscription_windows_enabled')}
          label="Read subscription usage from the provider"
          htmlFor="subscription-windows-enabled"
          info={<>
            OtoDock asks Claude and ChatGPT for each connected account's session and weekly usage.
            It prefers the account that resets first. Both usage windows show on the AI Engines
            cards. Off: the shared subscriptions balance by recorded cost only.
          </>}
        >
          <input
            id="subscription-windows-enabled"
            type="checkbox"
            checked={windowsEnabled}
            onChange={(e) => { const on = e.target.checked; setWindowsEnabled(on); save('subscription_windows_enabled', on, back((d) => setWindowsEnabled(d.subscription_windows_enabled !== false))) }}
            className="h-4 w-4 accent-action"
          />
        </SettingRow>
      </div>

      {/* Concurrency */}
      <div className={`${CARD} p-5 space-y-5`}>
        <div>
          <h3 className="text-sm font-semibold text-primary">Concurrency</h3>
          <p className="text-xs text-tertiary mt-0.5">
            Fully automatic. Agent sessions on this server start when there is free memory. That
            covers chat, phone, tasks and meetings. Under load, the oldest idle sessions close before any new
            one is refused. No tuning needed.
          </p>
        </div>

        {/* Read-only live memory gauge — admission is automatic, no knobs. */}
        <div className="space-y-2">
          <div className="text-sm font-medium text-primary">
            {stats?.sessions?.active ?? 0} active local session{(stats?.sessions?.active ?? 0) === 1 ? '' : 's'}
          </div>
          {stats?.sessions && (
            <>
              <p className="text-xs text-tertiary">
                Room for about <strong>{stats.sessions.fit_heavy}</strong> more heavy or{' '}
                <strong>{stats.sessions.fit_light}</strong> light session{stats.sessions.fit_light === 1 ? '' : 's'} right now.
              </p>
              <div className="h-2 rounded-full bg-surface-muted overflow-hidden" title="Reserved vs budget">
                <div
                  className="h-full bg-status-info-solid"
                  style={{ width: `${Math.min(100, Math.round(100 * stats.sessions.reserved_mb / Math.max(1, stats.sessions.budget_mb)))}%` }}
                />
              </div>
              <p className="text-2xs text-tertiary font-mono">
                {(stats.sessions.reserved_mb / 1024).toFixed(1)} / {(stats.sessions.budget_mb / 1024).toFixed(1)} GB reserved
                {' · '}{(stats.sessions.available_mb / 1024).toFixed(1)} GB free of {(stats.sessions.total_mb / 1024).toFixed(1)} GB
              </p>
            </>
          )}
          {stats?.by_surface && (
            <p className="text-2xs text-tertiary font-mono">
              chat {stats.by_surface.chat} · task {stats.by_surface.task} · meeting {stats.by_surface.meeting} · phone {stats.by_surface.phone}
            </p>
          )}
        </div>

        {/* The one remaining knob: the shared idle-reap timeout. */}
        <ConcurrencyRow
          label="Idle timeout in seconds"
          info="Close an idle chat or task session after this long. When memory runs low, idle sessions may close sooner."
          value={idleTimeout}
          min={60}
          forced={isForced('session_idle_timeout')}
          onChange={setIdleTimeout}
          onSave={() => { if (idleTimeout !== (data?.session_idle_timeout || '')) save('session_idle_timeout', idleTimeout) }}
          save={stateOf('session_idle_timeout')}
          fieldKey="session_idle_timeout"
        />
      </div>

      {/* Memory — capture & injection tuning. The platform-wide enable lives in
          code (memory is always on); users clear their own memory in their
          settings and managers clear an agent's memory in agent settings. */}
      {memory && (
        <div className={`${CARD} p-5 space-y-4`}>
          <div>
            <h3 className="text-sm font-semibold text-primary">Memory</h3>
            <p className="text-xs text-tertiary mt-0.5">
              Agents keep their memory with the <code>memory</code> tool. What they save
              reaches their prompt by itself.
            </p>
          </div>
          <div className="grid grid-cols-1 sm:grid-cols-2 gap-3">
            <div>
              <div className="mb-1">
                <label htmlFor="memory-inline-budget" className="text-xs text-secondary">Inline budget in bytes</label>
                <InfoTip label="Inline budget in bytes">
                  For each scope: below this size, topic files go into the prompt in full. Above
                  it, only their index goes in.
                </InfoTip>
                <SaveState state={stateOf('inline_budget_bytes')} />
              </div>
              <Input id="memory-inline-budget" type="number" min={1024} step={1024}
                value={memory.inline_budget_bytes}
                onChange={(e) => {
                  const bytes = parseInt(e.target.value || '8192', 10)
                  void run('inline_budget_bytes', () => saveMemory.mutateAsync({ inline_budget_bytes: bytes }), { failTitle: 'The memory setting was not saved' })
                }}
              />
            </div>
            <div>
              <div className="mb-1">
                <label htmlFor="memory-nudge-turns" className="text-xs text-secondary">Turns before a capture nudge</label>
                <InfoTip label="Turns before a capture nudge">
                  After this many chat turns without a memory save, the agent gets a short
                  reminder. It comes with the next message.
                </InfoTip>
                <SaveState state={stateOf('nudge_turns')} />
              </div>
              <Input id="memory-nudge-turns" type="number" min={0}
                value={memory.nudge_turns}
                onChange={(e) => {
                  const turns = parseInt(e.target.value || '0', 10)
                  void run('nudge_turns', () => saveMemory.mutateAsync({ nudge_turns: turns }), { failTitle: 'The memory setting was not saved' })
                }}
              />
              <span className="block text-3xs text-tertiary mt-1">0 turns it off.</span>
            </div>
          </div>
        </div>
      )}

      {/* Storage & Retention — hidden on the OtoDock cloud (operator owns disk
          policy: retention is forced ON at the default window server-side). */}
      {!data?.cloud && (
        <StorageRetentionCard
          enabled={retentionEnabled}
          days={retentionDays}
          onEnabledChange={(v) => { setRetentionEnabled(v); save('session_retention_enabled', v, back((d) => setRetentionEnabled(d.session_retention_enabled !== false))) }}
          onDaysChange={setRetentionDays}
          onSaveDays={() => { if (retentionDays !== data?.session_retention_days) save('session_retention_days', retentionDays) }}
          stateOf={stateOf}
        />
      )}

      {/* The archive of removed people: shown on the cloud too (its list and
          purge are an admin's; a forced setting shows disabled). */}
      <OffboardedArchivesCard
        enabled={archiveEnabled}
        days={archiveDays}
        onEnabledChange={(v) => { setArchiveEnabled(v); save('offboarded_retention_enabled', v, back((d) => setArchiveEnabled(d.offboarded_retention_enabled !== false))) }}
        onDaysChange={setArchiveDays}
        onSaveDays={saveArchiveDays}
        stateOf={stateOf}
        forcedKeys={data?.forced_keys || []}
      />

      {/* Storage Quotas — per-agent disk limits on local agent folders (hidden
          on cloud, where the operator pins them via OTODOCK_FORCED_SETTINGS). */}
      {!data?.cloud && (
        <StorageQuotasCard
          values={quotas}
          enforced={data?.storage_quotas_enforced === true}
          forcedKeys={data?.forced_keys || []}
          onChange={onQuotaChange}
          onSave={onQuotaSave}
          stateOf={stateOf}
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
        save={stateOf('mcp_auto_update_enabled')}
        onToggle={(v) => { setMcpAutoUpdate(v); save('mcp_auto_update_enabled', v, back((d) => setMcpAutoUpdate(d.mcp_auto_update_enabled !== false))) }}
      />
    </div>
  )
}
