import { useState } from 'react'
import { useEnableMcp, useDisableMcp, useSetMcpConfig, useSetMcpToolFilter, useSetHostedServiceMode, useSetNetworkAccess, useDockerAction, useDeleteMcp, useUpdateMcp, useAcceptMcpSource, useDismissMcpSourceChange, isUpdateOffer, automaticUpdateNote, McpServer, McpUpdateInfo, McpSourceChange, McpSourceEnd } from '../../api/mcps'
import { INPUT_MULTILINE_SECRET, isGitHubApp } from '../../lib/kinds/oauthApp'
import { useAgents } from '../../api/agents'
import { useSetInfraCredentials, useCredentialSchema } from '../../api/credentials'
import { useClientRegistrations, useForgetClientRegistration, ClientRegistration } from '../../api/oauth'
import McpInstanceManager, { ApiKeyRelayInfo } from '../../components/admin/McpInstanceManager'
import McpIcon from '../../components/McpIcon'
import { useAuth } from '../../contexts/AuthContext'
import { formatRelativeTime } from '../../lib/format'
import { safeHref } from '../../lib/safeUrl'
import { isContainerRuntime } from '../../lib/kinds/mcpRuntime'
import { DOCKER_STATUS as DOCKER, ENABLE_DOCKER, type DockerStatus } from '../../lib/status/docker'
import { useDialogs } from '../../components/ui/dialogs'
import { toastError } from '../../lib/http/apiJson'
import Button from '../../components/ui/Button'
import { Input } from '../../components/ui/Field'
import { BADGE_BASE, BADGE_TONE, BUTTON_BASE, BUTTON_SIZE, CARD, CARD_BORDERED, FIELD, FIELD_BORDER, FIELD_COMPACT, SEGMENT } from '../../components/ui/classes'
import Switch from '../../components/ui/Switch'
import InfoTip from '../../components/ui/InfoTip'
import { SaveState } from '../../components/ui/SettingsControls'
import { useSaveState } from '../../hooks/useSaveState'

// A label and a colour per container status (lib/status/docker.ts).
const DOCKER_STATUS: Record<DockerStatus, { label: string; color: string }> = {
  running: { label: 'Running', color: 'text-status-success-fg' },
  // Container is up but failing its healthcheck (e.g. a wedged camoufox) — amber,
  // not green. The proxy never auto-restarts Docker MCPs, so it's operator-visible.
  unhealthy: { label: 'Unhealthy', color: 'text-status-warning-fg' },
  starting: { label: 'Starting…', color: 'text-status-warning-fg' },
  stopped: { label: 'Stopped', color: 'text-status-danger-fg' },
  not_found: { label: 'Not Found', color: 'text-tertiary' },
  error: { label: 'Error', color: 'text-status-danger-fg' },
  unknown: { label: 'Unknown', color: 'text-tertiary' },
  not_checked: { label: '...', color: 'text-tertiary' },
}

function InstanceManagerWrapper({ mcpName, apiKeyRelay }: { mcpName: string; apiKeyRelay?: ApiKeyRelayInfo }) {
  const { data: agentList } = useAgents({ all: true })
  const agents = agentList?.map(a => a.name) || []
  const { authConfig } = useAuth()
  return <McpInstanceManager mcpName={mcpName} agents={agents} apiKeyRelay={apiKeyRelay}
    airGapped={!!authConfig?.air_gapped} relayAvailable={!!authConfig?.relay_available} />
}

// A source as the card shows it: a link when it is a page (npm, PyPI, a
// repository: an absolute http(s) URL), plain text for an image reference
// or a host (safeHref alone would read those as paths of this origin).
function SourceLink({ end }: { end: McpSourceEnd }) {
  const href = /^https?:\/\//i.test(end.url || '') ? safeHref(end.url) : undefined
  const text = end.url || 'No source'
  return href ? (
    <a href={href} target="_blank" rel="noreferrer" className="font-mono underline hover:no-underline break-all"
      onClick={e => e.stopPropagation()}>{text}</a>
  ) : <span className="font-mono break-all">{text}</span>
}

function KeyList({ label, keys, tone }: { label: string; keys: string[]; tone?: 'warn' }) {
  if (keys.length === 0) return null
  return (
    <p className={`text-2xs ${tone === 'warn' ? 'text-status-warning-fg' : 'text-secondary'}`}>
      <span className="text-secondary">{label}:</span>{' '}
      {keys.map(k => <code key={k} className="mr-1">{k}</code>)}
    </p>
  )
}

// The install's client registrations at the authorization server an MCP
// server names: the live one, the older ones, and Forget (the row is
// revoked, the next connect registers afresh). A server that also takes
// the vendor's app tokens makes this the fallback sign-in, offered under
// the app credentials: tools only, since a token issued for the MCP server
// serves no webhook call.
function RegisteredClientCard({ mcp, issuer, resourceHost, confidential, acceptsAppTokens, hostedActive, rows, onForget, forgetting }: {
  mcp: McpServer; issuer: string; resourceHost: string; confidential: boolean; acceptsAppTokens: boolean
  hostedActive: boolean; rows: ClientRegistration[]; onForget: (id: number) => void; forgetting: boolean
}) {
  const dialogs = useDialogs()
  const where = issuer ? (issuer.replace(/^https:\/\//, '')) : resourceHost
  const live = rows.filter(r => !r.revoked_at)
  const older = rows.filter(r => !!r.revoked_at)
  return (
    <div className={`${CARD_BORDERED} p-3`}>
      <div className="flex items-center justify-between mb-1.5">
        <p className="text-2xs font-semibold text-tertiary uppercase tracking-wide">
          {acceptsAppTokens ? 'Sign in without an app' : `Signs in at ${where}`}
        </p>
        <span className={`${BADGE_BASE} ${BADGE_TONE.neutral}`}>
          {confidential ? 'Confidential client' : 'Public client'}
        </span>
      </div>
      {acceptsAppTokens ? (
        <p className="text-xs text-secondary mb-2">
          {mcp.label}'s server also accepts this install as its own client. With hosted
          mode off and no app credentials, people sign in at {where}. They get tools
          only, no events.
          {hostedActive && ' Hosted via OtoDock is on, so OtoDock signs people in.'}
          {!hostedActive && mcp.app_credential_configured && ' App credentials are configured, so the admin app signs people in and keeps events.'}
        </p>
      ) : (
        <p className="text-xs text-secondary mb-2">
          {mcp.label}'s server has its own sign-in service and accepts no other. This
          install registers there the first time someone connects. People sign in on the
          vendor's page, and no OAuth app is created.
        </p>
      )}
      {live.length === 0 && older.length === 0 && (
        <p className="text-2xs text-tertiary italic">No registration yet: the first connect creates one.</p>
      )}
      {live.map(r => (
        <div key={r.id} className="text-2xs text-secondary space-y-0.5 mb-2" data-testid="registration-live">
          <p><span className="text-tertiary">Client id:</span> <code>{r.client_id}</code>{r.has_secret ? ', with a secret' : ''}</p>
          <p><span className="text-tertiary">Callback:</span> <code className="break-all">{r.redirect_uri}</code></p>
          <p>
            <span className="text-tertiary">Registered</span> {formatRelativeTime(r.created_at)}
            {r.last_used_at && <>, <span className="text-tertiary">used</span> {formatRelativeTime(r.last_used_at)}</>}
          </p>
          <Button variant="secondary" size="xs"
            onClick={async () => {
              if (await dialogs.confirm({
                title: `Forget this install's registration at ${where}?`,
                body: `${r.has_secret ? 'Accounts connected through it will need a reconnect' : 'Connected accounts keep working'}. The next connect registers again.`,
                confirmLabel: 'Forget',
                tone: 'danger',
              })) onForget(r.id)
            }}
            disabled={forgetting}
          >
            Forget
          </Button>
        </div>
      ))}
      {older.length > 0 && (
        <p className="text-3xs text-tertiary italic">
          {older.length} older registration{older.length === 1 ? '' : 's'} kept for the accounts that still use {older.length === 1 ? 'it' : 'them'}.
        </p>
      )}
    </div>
  )
}

// The pending change of a community MCP's catalog source: what moved,
// whether the catalog entry declares it, what happens to the credentials,
// and the Switch (proxy COMMUNITY-MARKETPLACE.md "Source changes").
function SourceChangeCard({ mcp, change }: { mcp: McpServer; change: McpSourceChange }) {
  const dialogs = useDialogs()
  const accept = useAcceptMcpSource()
  const [log, setLog] = useState('')
  const creds = change.plan?.credentials
  const runtimeChanged = !!change.plan?.runtime && change.plan.runtime.from !== change.plan.runtime.to
  const renames = Object.entries(creds?.rename ?? {})
  const switching = change.status === 'switching' || accept.isPending
  const confirmText =
    `${change.from.url}\n  →  ${change.to.url}\n\n` +
    'The MCP keeps its name, settings, credentials and agent assignments. ' +
    (change.declared ? '' : 'The community list does not declare this change: verify the new source first. ') +
    'It is reinstalled from the new source.'
  return (
    <div className={`rounded-card border p-3 ${change.declared
      ? 'border-status-info-border bg-status-info-bg'
      : 'border-status-warning-border bg-status-warning-bg'}`}>
      <div className="flex items-start justify-between gap-3 mb-2">
        <div className="min-w-0">
          <p className={`text-xs font-medium ${change.declared ? 'text-status-info-fg' : 'text-status-warning-fg'}`}>
            Source change
          </p>
          <p className="text-2xs text-secondary">
            <span className="text-secondary">Installed:</span> <SourceLink end={change.from} />
            {change.from.runtime && <span className="text-secondary"> ({change.from.runtime})</span>}
          </p>
          <p className="text-2xs text-secondary">
            <span className="text-secondary">Community list:</span> <SourceLink end={change.to} />
            {change.to.runtime && <span className="text-secondary"> ({change.to.runtime})</span>}
            {change.to.version && <span className="text-secondary"> v{change.to.version}</span>}
          </p>
        </div>
        <Button variant={change.declared ? 'primary' : 'secondary'} size="xs" className="shrink-0"
          onClick={async () => {
            if (!(await dialogs.confirm({ title: `Switch "${mcp.label}" to its new source?`, body: confirmText, confirmLabel: 'Switch' }))) return
            setLog('')
            accept.mutate({ name: mcp.name, from: change.from.url, to: change.to.url, manifest_hash: change.to_manifest_hash }, {
              onSuccess: (data) => setLog(data.install_log || 'Switched.'),
              onError: (e: Error) => setLog(`Error: ${e.message}`),
            })
          }}
          disabled={switching}
        >
          {switching ? 'Switching…' : 'Switch'}
        </Button>
      </div>
      <p className="text-2xs text-secondary mb-1.5">
        {change.declared
          ? 'The community list entry says it replaces the installed source.'
          : 'The community list entry does not say it replaces the installed source. The entry may have been tampered with. Verify the new source before you switch.'}
        {' '}Automatic updates never apply a source change.
      </p>
      {runtimeChanged && (
        <p className="text-2xs text-secondary">
          <span className="text-secondary">Runtime:</span> {change.plan!.runtime!.from} → {change.plan!.runtime!.to}
        </p>
      )}
      {creds && (
        <div className="mt-1.5 space-y-0.5">
          <KeyList label="Credentials that carry" keys={creds.carry} />
          {renames.length > 0 && (
            <p className="text-2xs text-secondary">
              <span className="text-secondary">Renamed:</span>{' '}
              {renames.map(([o, n]) => <span key={o} className="mr-1.5"><code>{o}</code> → <code>{n}</code></span>)}
            </p>
          )}
          <KeyList label="Reconnect needed" keys={creds.reconnect} tone="warn" />
          {creds.oauth === 'reconnect' && (
            <p className="text-2xs text-status-warning-fg">
              The connected accounts need a reconnect: the new source uses another sign-in provider.
            </p>
          )}
          {creds.oauth === 'carry' && (
            <p className="text-2xs text-secondary"><span className="text-secondary">Connected accounts:</span> carry over.</p>
          )}
        </div>
      )}
      {change.plan?.bearer_host_change && (
        <p className="mt-1 text-2xs text-status-warning-fg">
          Add the new host to the OAuth Bearer Allowlist in Setup, Security. Agents cannot use it until then.
        </p>
      )}
      {change.result?.error && (
        <p className="mt-1.5 text-2xs text-status-danger-fg">
          Last attempt failed{change.result.failed_at ? ` ${formatRelativeTime(change.result.failed_at)}` : ''}: {change.result.error}
        </p>
      )}
      {log && (
        <details open={log.startsWith('Error')} className="mt-1.5">
          <summary className="text-2xs text-secondary cursor-pointer">Switch log</summary>
          <pre className="mt-1 text-2xs text-secondary bg-surface-muted rounded-control p-2 overflow-x-auto max-h-32 overflow-y-auto whitespace-pre-wrap">{log}</pre>
        </details>
      )}
    </div>
  )
}

// The record of a finished switch, shown until dismissed.
function SwitchedCard({ mcp, change }: { mcp: McpServer; change: McpSourceChange }) {
  const dismiss = useDismissMcpSourceChange()
  const result = change.result || {}
  const renames = Object.entries(result.renamed ?? {})
  return (
    <div className="rounded-card border border-status-success-border bg-status-success-bg p-3">
      <div className="flex items-start justify-between gap-3">
        <div className="min-w-0">
          <p className="text-xs font-medium text-status-success-fg">
            Switched to <SourceLink end={change.to} />
            {result.switched_at && <span className="text-secondary font-normal"> {formatRelativeTime(result.switched_at)}</span>}
          </p>
          {renames.length > 0 && (
            <p className="text-2xs text-secondary">
              <span className="text-secondary">Renamed:</span>{' '}
              {renames.map(([o, n]) => <span key={o} className="mr-1.5"><code>{o}</code> → <code>{n}</code></span>)}
            </p>
          )}
          <KeyList label="Kept under the old key" keys={result.kept ?? []} />
          <KeyList label="Reconnect needed" keys={result.reconnect_needed ?? []} tone="warn" />
          {result.oauth === 'reconnect' && (
            <p className="text-2xs text-status-warning-fg">The connected accounts need a reconnect.</p>
          )}
          {result.container_started === false && (
            <p className="text-2xs text-status-warning-fg">
              The new container did not start. Use the Docker controls to start it.
            </p>
          )}
          {result.error && (
            // The half of a switch after the install cannot be undone, so an
            // error there is recorded on the switched row, never hidden.
            <p className="text-2xs text-status-danger-fg">
              Finished with an error: {result.error}
            </p>
          )}
        </div>
        <Button variant="secondary" size="xs"
          onClick={() => { dismiss.mutateAsync(mcp.name).catch(toastError('The notice was not dismissed')) }}
          disabled={dismiss.isPending}
          className="shrink-0"
        >
          Dismiss
        </Button>
      </div>
    </div>
  )
}

export function McpRow({ mcp, updateInfo, autoUpdateEnabled = true }: {
  mcp: McpServer; updateInfo?: McpUpdateInfo; autoUpdateEnabled?: boolean
}) {
  const dialogs = useDialogs()
  const [expanded, setExpanded] = useState(false)
  const [configValues, setConfigValues] = useState<Record<string, string>>(mcp.config_values || {})
  const [credValues, setCredValues] = useState<Record<string, string>>({})
  const [credError, setCredError] = useState('')
  const [appCredValues, setAppCredValues] = useState<Record<string, string>>({})
  // The kind of vendor application the admin registers (lib/kinds/oauthApp.ts):
  // the bundle's kind, else the manifest's first.
  const appKinds = mcp.app_kinds?.kinds ?? []
  const [appKindPick, setAppKindPick] = useState('')
  const appKind = appKindPick || mcp.app_credential_kind || appKinds[0]?.kind || ''
  const [appCredError, setAppCredError] = useState('')
  const { stateOf, run } = useSaveState()
  const enable = useEnableMcp()
  const disable = useDisableMcp()
  const saveConfig = useSetMcpConfig()
  const saveToolFilter = useSetMcpToolFilter()
  const dockerAction = useDockerAction()
  const saveInfraCreds = useSetInfraCredentials()
  const setHostedMode = useSetHostedServiceMode()
  const setNetworkAccess = useSetNetworkAccess()
  const { authConfig } = useAuth()
  const deleteMcp = useDeleteMcp()
  const updateMcp = useUpdateMcp()
  // Hosted OAuth: effective mode = admin override (_hosted_service_mode)
  // or the manifest default. It only takes effect (relay-routed, no install-side
  // credential) when the install is relay-licensed — mirrors the server-side
  // `hosted_oauth_active` gate. When inactive the self-managed app-cred form shows.
  const hostedOauthMode = mcp.hosted_oauth_mode || mcp.hosted?.oauth_app?.default_mode
  const hostedOauthActive = hostedOauthMode === 'hosted' && !authConfig?.air_gapped
  // The MCP server names its own authorization server (the credential
  // schema carries the manifest's declaration; the install's registrations
  // there are an admin route, read only when the row is expanded).
  const schema = useCredentialSchema(expanded && mcp.credential_type === 'per_user')
  const authServer = schema.data?.[mcp.name]?.oauth_meta?.authorization_server
  const registrations = useClientRegistrations(expanded && !!authServer)
  const forgetRegistration = useForgetClientRegistration()
  // A server that takes its own authorization server's tokens only: the
  // registered client is the one sign-in, so the row hides the hosted
  // choice and the app credentials.
  const ownServerOnly = !!authServer && !authServer.accepts_app_tokens
  const registeredClientCard = authServer && (
    <RegisteredClientCard
      mcp={mcp}
      issuer={authServer.issuer}
      resourceHost={authServer.resource_host}
      confidential={authServer.confidential}
      acceptsAppTokens={authServer.accepts_app_tokens}
      hostedActive={hostedOauthActive}
      rows={(registrations.data || []).filter(r => r.mcps.includes(mcp.name))}
      onForget={(id) => { forgetRegistration.mutateAsync(id).catch(toastError('The registration was not forgotten')) }}
      forgetting={forgetRegistration.isPending}
    />
  )
  const [updateLog, setUpdateLog] = useState('')
  const [toolFilterRegex, setToolFilterRegex] = useState(mcp.tool_filter_regex || '')
  const [toolFilterError, setToolFilterError] = useState('')

  // The kill-switchable core MCPs (meetings/delegation) stay toggleable —
  // the server's can_disable is the authority, not the category.
  const lockedOn = !mcp.can_disable
  // docker_managed === false: the container is an operator-owned sibling of
  // the platform's own compose stack (core file-tools on containerized
  // installs) — the proxy can't report or drive it, so the status pill and
  // start/stop/restart controls are hidden rather than showing "Not Found".
  const isDocker = isContainerRuntime(mcp.runtime) && mcp.docker_managed !== false
  const hasConfig = mcp.config_fields.length > 0
  const hasSkills = mcp.skills.length > 0
  const dockerInfo = isDocker ? DOCKER_STATUS[mcp.docker_status || DOCKER.NOT_CHECKED] : null
  // An update replaced the image but the container still serves the old one.
  const restartToSwitch = isDocker && mcp.docker_status === DOCKER.RUNNING && !!mcp.docker_image_stale
  // What the last check says about this MCP: a pending source change
  // replaces the ordinary offer; a finished switch rides beside one; an
  // install ahead of the catalog is a note, not an offer.
  const change = updateInfo?.source_change
  const pendingChange = change && change.status !== 'switched' ? change : undefined
  const switchedChange = change && change.status === 'switched' ? change : undefined
  const ahead = updateInfo?.reason === 'ahead' ? updateInfo : undefined
  const ordinaryUpdate = updateInfo && isUpdateOffer(updateInfo) && updateInfo.reason !== 'source' ? updateInfo : undefined

  const handleToggle = () => {
    if (lockedOn) return
    if (mcp.enabled) {
      void run('enabled', () => disable.mutateAsync(mcp.name), { failTitle: 'The MCP was not turned off' })
    } else {
      void run('enabled', () => enable.mutateAsync(mcp.name).then((result) => {
        // Surface Docker container start failures — previously silent.
        // ``docker_status`` is null for non-Docker MCPs; "started" on
        // success; "failed" with ``docker_error`` when the compose call
        // exited non-zero (Docker daemon down, image build failed, port
        // conflict, etc.).
        if (result?.docker_status === ENABLE_DOCKER.FAILED) {
          const msg = result.docker_error || 'Docker container failed to start.'
          void dialogs.alert({
            title: `The container of "${mcp.name}" did not start`,
            body: `The MCP is turned on, but its Docker container did not start.\n\n${msg}\n\n` +
              'Fix the issue, then press Start in the Docker controls below to retry.',
          })
        }
      }), { failTitle: 'The MCP was not turned on' })
    }
  }

  const handleSaveConfig = () => {
    void run('config', () => saveConfig.mutateAsync({ name: mcp.name, values: configValues }),
      { failTitle: 'The configuration was not saved' })
  }

  return (
    <div className={`${CARD} overflow-hidden`}>
      {/* Header row */}
      <div
        role="button"
        tabIndex={0}
        aria-expanded={expanded}
        className="flex items-center gap-3 px-4 py-3 cursor-pointer hover:bg-surface-hover transition-colors focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-inset focus-visible:ring-focus-ring"
        onClick={() => setExpanded(!expanded)}
        onKeyDown={(e) => {
          if (e.target !== e.currentTarget) return
          if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); setExpanded(!expanded) }
        }}
      >
        {/* Enable/disable toggle; its click stays here, or the row would fold. */}
        <span className="inline-flex shrink-0" onClick={(e) => e.stopPropagation()}>
          <Switch
            aria-label={`${mcp.label} enabled`}
            checked={mcp.enabled}
            onChange={() => handleToggle()}
            disabled={lockedOn || enable.isPending || disable.isPending}
            title={lockedOn ? 'Core MCP, always on' : mcp.enabled ? 'Disable' : 'Enable'}
            // A core MCP is on and locked: dimmed less, so it still reads as on.
            className={lockedOn ? 'disabled:opacity-70' : ''}
          />
        </span>

        <McpIcon name={mcp.name} label={mcp.label} category={mcp.category} author={mcp.author} hasIcon={mcp.icon} />

        <div className="flex-1 min-w-0">
          <div className="flex items-center gap-2 flex-wrap">
            <span className="font-medium text-sm text-primary">{mcp.label}<SaveState state={stateOf('enabled')} /></span>
            {mcp.assignment_mode === 'explicit' && (
              <span className={`${BADGE_BASE} ${BADGE_TONE.warning}`}>
                Explicit assignment
              </span>
            )}
            {pendingChange && (
              <span className={`${BADGE_BASE} ${BADGE_TONE[pendingChange.declared ? 'info' : 'warning']} animate-pulse`}>
                {pendingChange.status === 'switching' ? 'Switching source…' : 'Source changed'}
              </span>
            )}
            {ordinaryUpdate && (
              <span className={`${BADGE_BASE} ${BADGE_TONE.info} animate-pulse`}>
                {ordinaryUpdate.reason === 'manifest' ? 'Integration update' : `${ordinaryUpdate.latest} available`}
              </span>
            )}
          </div>
        </div>

        {/* Docker status */}
        {dockerInfo && (
          <span className={`text-xs font-medium ${restartToSwitch ? 'text-status-warning-fg' : dockerInfo.color} shrink-0`}>
            {restartToSwitch ? `${dockerInfo.label}, restart to switch` : dockerInfo.label}
          </span>
        )}

        {/* Expand chevron */}
        <svg className={`w-4 h-4 text-tertiary transition-transform shrink-0 ${expanded ? 'rotate-180' : ''}`} fill="none" stroke="currentColor" viewBox="0 0 24 24">
          <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M19 9l-7 7-7-7" />
        </svg>
      </div>

      {/* Expanded detail */}
      {expanded && (
        <div className="border-t border-subtle">
          {/* Two-column grid on desktop, single on mobile */}
          <div className="grid grid-cols-1 md:grid-cols-2 gap-0 md:gap-4 p-4">
            {/* LEFT: Info */}
            <div className="space-y-3 mb-4 md:mb-0">
              {/* Description & meta */}
              <div className={`${CARD_BORDERED} p-3`}>
                <p className="text-sm text-secondary leading-relaxed">{mcp.description}</p>
                <div className="mt-2 flex flex-wrap gap-x-4 gap-y-1 text-2xs text-tertiary">
                  <span>v{mcp.version || 'latest'}</span>
                  {/* Where the server code comes from: the catalog entry's upstream
                      project for community MCPs; core and custom ship with OtoDock. */}
                  {mcp.category === 'community' ? (
                    mcp.author && (
                      <span>
                        From{' '}
                        {safeHref(mcp.author_url) ? (
                          <a
                            href={safeHref(mcp.author_url)}
                            target="_blank"
                            rel="noreferrer"
                            className="text-link hover:underline"
                            onClick={e => e.stopPropagation()}
                          >
                            {mcp.author}
                          </a>
                        ) : mcp.author}
                      </span>
                    )
                  ) : (
                    <span>Built into OtoDock</span>
                  )}
                  {mcp.source && <span className="font-mono truncate max-w-48">{mcp.source}</span>}
                </div>
              </div>

              {/* Agents */}
              {mcp.agents.length > 0 && (
                <div className={`${CARD_BORDERED} p-3`}>
                  <p className="text-2xs font-semibold text-tertiary uppercase tracking-wide mb-2">Enabled for</p>
                  <div className="flex gap-1.5 flex-wrap">
                    {mcp.agents.map(a => (
                      <span key={a} className={`${BADGE_BASE} ${BADGE_TONE.neutral}`}>{a}</span>
                    ))}
                  </div>
                </div>
              )}

              {/* Skills */}
              {hasSkills && (
                <div className={`${CARD_BORDERED} p-3`}>
                  <p className="text-2xs font-semibold text-tertiary uppercase tracking-wide mb-2">Skills</p>
                  <div className="space-y-1">
                    {mcp.skills.map(s => (
                      <p key={s.id} className="text-xs text-secondary">
                        <span className="font-mono text-primary">{s.id}</span>
                        {s.description && <span className="ml-1.5 text-tertiary">· {s.description}</span>}
                      </p>
                    ))}
                  </div>
                </div>
              )}

              {/* Per-user credentials note */}
              {mcp.credential_type === 'per_user' && (
                <div className="rounded-card border border-status-warning-border bg-status-warning-bg p-3">
                  <p className="text-xs text-status-warning-fg">Each person adds their own credentials in User Settings, Integrations.</p>
                </div>
              )}

              {/* A pending source change replaces the ordinary offer */}
              {pendingChange && <SourceChangeCard mcp={mcp} change={pendingChange} />}

              {/* A finished switch, until dismissed */}
              {switchedChange && <SwitchedCard mcp={mcp} change={switchedChange} />}

              {/* Ahead of the catalog: a note and an explicit way back */}
              {ahead && (
                <div className={`${CARD_BORDERED} p-3`}>
                  <div className="flex items-center justify-between gap-3">
                    <p className="text-2xs text-secondary">
                      Installed {ahead.current} is newer than the community version {ahead.latest}. Automatic updates leave it alone.
                    </p>
                    <Button variant="secondary" size="xs"
                      onClick={async () => {
                        if (!(await dialogs.confirm({
                          title: `Revert "${mcp.label}" to the community version ${ahead.latest}?`,
                          body: 'Its settings, credentials and agent assignments are kept.',
                          confirmLabel: 'Revert',
                        }))) return
                        setUpdateLog('')
                        updateMcp.mutate(mcp.name, {
                          onSuccess: (data) => setUpdateLog(data.install_log || 'Reverted.'),
                          onError: (e: Error) => setUpdateLog(`Error: ${e.message}`),
                        })
                      }}
                      disabled={updateMcp.isPending}
                      className="shrink-0"
                    >
                      {updateMcp.isPending ? 'Reverting…' : `Revert to ${ahead.latest}`}
                    </Button>
                  </div>
                  {updateLog && (
                    <details open={updateLog.startsWith('Error')} className="mt-1.5">
                      <summary className="text-2xs text-tertiary cursor-pointer">Install log</summary>
                      <pre className="mt-1 text-2xs text-tertiary bg-surface-muted rounded-control p-2 overflow-x-auto max-h-32 overflow-y-auto whitespace-pre-wrap">{updateLog}</pre>
                    </details>
                  )}
                </div>
              )}

              {/* Update available */}
              {ordinaryUpdate && (
                <div className="rounded-card border border-status-info-border bg-status-info-bg p-3">
                  <div className="flex items-center justify-between mb-2">
                    <div>
                      <p className="text-xs font-medium text-status-info-fg">Update Available</p>
                      <p className="text-2xs text-secondary">
                        {ordinaryUpdate.reason === 'manifest'
                          ? (ordinaryUpdate.rebuild
                              ? 'Rebuild: its Python setup cannot run for agents'
                              : 'Integration update: its package details changed')
                          : <>{ordinaryUpdate.current} → {ordinaryUpdate.latest}</>}
                        <span className="text-secondary ml-1">({ordinaryUpdate.registry})</span>
                      </p>
                      {automaticUpdateNote(ordinaryUpdate, autoUpdateEnabled) && (
                        <p className="text-2xs text-secondary mt-0.5">
                          {automaticUpdateNote(ordinaryUpdate, autoUpdateEnabled)}
                        </p>
                      )}
                    </div>
                    {/* A catalog git+ source at a tag or a branch: the catalog install refuses it. */}
                    {!(ordinaryUpdate.registry === 'git' && ordinaryUpdate.pinned_commit === false) && (
                      <Button variant="primary" size="xs"
                        onClick={() => {
                          setUpdateLog('')
                          updateMcp.mutate(mcp.name, {
                            onSuccess: (data) => setUpdateLog(data.install_log || 'Updated successfully'),
                            onError: (e: Error) => setUpdateLog(`Error: ${e.message}`),
                          })
                        }}
                        disabled={updateMcp.isPending}
                      >
                        {updateMcp.isPending ? 'Updating...' : 'Update'}
                      </Button>
                    )}
                  </div>
                  {updateLog && (
                    <details open={updateLog.startsWith('Error')}>
                      <summary className="text-2xs text-secondary cursor-pointer">Install log</summary>
                      <pre className="mt-1 text-2xs text-tertiary bg-surface-muted rounded-control p-2 overflow-x-auto max-h-32 overflow-y-auto whitespace-pre-wrap">{updateLog}</pre>
                    </details>
                  )}
                </div>
              )}

              {/* Delete MCP (community only — custom MCPs ship with the platform and aren't re-installable) */}
              {mcp.category === 'community' && (
                <div className="rounded-card border border-status-danger-border bg-status-danger-bg p-3">
                  <div className="flex items-center justify-between">
                    <div>
                      <p className="text-xs font-medium text-status-danger-fg">Delete MCP</p>
                      <p className="text-2xs text-secondary">Removes its folder, settings, credentials and agent assignments. Reinstall it later from Browse Community MCPs.</p>
                    </div>
                    <Button variant="danger" size="xs"
                      onClick={async () => {
                        if (await dialogs.confirm({
                          title: `Delete "${mcp.label}"?`,
                          body: 'This removes the MCP folder, settings, credentials and agent assignments. This cannot be undone.',
                          confirmLabel: 'Delete',
                          tone: 'danger',
                        })) {
                          deleteMcp.mutateAsync(mcp.name).catch(toastError('The MCP was not deleted'))
                        }
                      }}
                      disabled={deleteMcp.isPending}
                    >
                      {deleteMcp.isPending ? 'Deleting...' : 'Delete'}
                    </Button>
                  </div>
                </div>
              )}
            </div>

            {/* RIGHT: Actionable sections */}
            <div className="space-y-3">
              {/* Docker controls */}
              {isDocker && (
                <div className={`${CARD_BORDERED} p-3`}>
                  <p className="text-2xs font-semibold text-tertiary uppercase tracking-wide mb-2">Docker</p>
                  {restartToSwitch && (
                    <p className="text-2xs text-status-warning-fg mb-2">
                      Updated to v{mcp.version}. The container still runs the previous
                      image: restart to switch.
                    </p>
                  )}
                  <div className="flex items-center gap-2">
                    <Button variant="secondary" size="xs"
                      onClick={() => dockerAction.mutate({ name: mcp.name, action: 'start' })}
                      disabled={dockerAction.isPending || mcp.docker_status === DOCKER.RUNNING}
                    >
                      Start
                    </Button>
                    <Button variant="danger" size="xs"
                      onClick={() => dockerAction.mutate({ name: mcp.name, action: 'stop' })}
                      disabled={dockerAction.isPending || mcp.docker_status === DOCKER.STOPPED}
                    >
                      Stop
                    </Button>
                    <Button variant="secondary" size="xs"
                      onClick={() => dockerAction.mutate({ name: mcp.name, action: 'restart' })}
                      disabled={dockerAction.isPending}
                    >
                      Restart
                    </Button>
                  </div>
                  {dockerAction.error && (
                    <p className="text-2xs text-status-danger-fg mt-2">{dockerAction.error.message}</p>
                  )}
                </div>
              )}

              {/* Config fields */}
              {hasConfig && (
                <div className={`${CARD_BORDERED} p-3`}>
                  <p className="text-2xs font-semibold text-tertiary uppercase tracking-wide mb-2">Configuration</p>
                  <div className="space-y-2.5">
                    {mcp.config_fields.map(f => (
                      <div key={f.key}>
                        <label className="block text-xs text-secondary mb-1">{f.label}</label>
                        <Input compact
                          type={f.input_type === 'password' ? 'password' : 'text'}
                          value={configValues[f.key] ?? f.default ?? ''}
                          onChange={(e) => setConfigValues({ ...configValues, [f.key]: e.target.value })}
                          placeholder={f.default || ''}
                        />
                      </div>
                    ))}
                    <div className="flex items-center gap-2 pt-1">
                      <Button variant="primary" size="xs"
                        onClick={handleSaveConfig}
                        disabled={saveConfig.isPending}
                      >
                        {saveConfig.isPending ? 'Saving...' : 'Save'}
                      </Button>
                      <SaveState state={stateOf('config')} besideButton />
                    </div>
                  </div>
                </div>
              )}

              {/* Internal-network access (homelab MCPs declaring network_targets).
                  When on, the agent sandbox carves egress to exactly this MCP's
                  configured target host — nothing else on the LAN. Unavailable
                  on hosted OtoDock (no operator network). */}
              {mcp.has_network_targets && (
                <div className={`${CARD_BORDERED} p-3`}>
                  <p className="text-2xs font-semibold text-tertiary uppercase tracking-wide mb-2">Internal network access</p>
                  {authConfig?.cloud ? (
                    <p className="text-xs text-secondary leading-snug">
                      Not available on OtoDock Cloud. This MCP can only reach
                      public addresses.
                    </p>
                  ) : (
                    <>
                      <div className="relative flex items-start gap-2">
                        <input
                          id={`network-access-${mcp.name}`}
                          type="checkbox"
                          checked={mcp.network_access ?? true}
                          disabled={setNetworkAccess.isPending}
                          onChange={(e) => {
                            const enabled = e.target.checked
                            void run('network', () => setNetworkAccess.mutateAsync({ name: mcp.name, enabled }), { failTitle: 'The network access setting was not saved' })
                          }}
                          className="mt-0.5 accent-action cursor-pointer"
                        />
                        <div className="text-xs text-primary">
                          <label htmlFor={`network-access-${mcp.name}`} className="cursor-pointer after:absolute after:inset-0">Allow this MCP to reach its configured address on your network</label>
                          <span className="relative z-10">
                            <InfoTip label="Allow this MCP to reach its configured address on your network">Agents are cut off from your network by default.</InfoTip>
                          </span>
                          <SaveState state={stateOf('network')} />
                        </div>
                      </div>
                      <p className="text-2xs text-secondary leading-snug mt-1.5">This opens a path to the host you set above, and nothing else.</p>
                    </>
                  )}
                </div>
              )}

              {/* Tool Filter. Generic per-MCP
                  runtime tool restriction — rendered for every MCP, but
                  enabled only when manifest declares `tool_filter`. */}
              <div className={`${CARD_BORDERED} p-3`}>
                <div className="flex items-center justify-between mb-1.5">
                  <div className="min-w-0">
                    <p className="inline text-2xs font-semibold text-tertiary uppercase tracking-wide">Tool Filter</p>
                    {mcp.tool_filter_supported && <InfoTip label="Tool Filter">Limit which tools this MCP gives agents.</InfoTip>}
                  </div>
                  {!mcp.tool_filter_supported && (
                    <span
                      className="text-3xs text-tertiary italic"
                      title="This MCP does not support a tool filter."
                    >
                      Not supported by this MCP
                    </span>
                  )}
                </div>
                <p className="text-xs text-secondary mb-2">
                  {mcp.tool_filter_supported
                    ? <>Leave empty to allow every tool. Or enter a pattern for the tool names to keep. For example, <code className="font-mono">mail</code> keeps any tool whose name contains “mail”. And <code className="font-mono">^(mail|calendar)_.*</code> keeps only the mail and calendar tools.</>
                    : 'This MCP always gives agents all of its tools.'}
                </p>
                <Input compact
                  type="text"
                  value={toolFilterRegex}
                  onChange={(e) => setToolFilterRegex(e.target.value)}
                  disabled={!mcp.tool_filter_supported}
                  placeholder={
                    mcp.tool_filter_supported
                      ? "^(mail|calendar)_.*"
                      : 'Not supported'
                  }
                  className="font-mono"
                />
                {mcp.tool_filter_supported && isDocker && (
                  <p className="text-xs text-tertiary mt-1">Saving the filter restarts its container.</p>
                )}
                {mcp.tool_filter_supported && (
                  <div className="flex items-center gap-2 pt-2">
                    <Button variant="primary" size="xs"
                      onClick={() => {
                        setToolFilterError('')
                        void run('tool_filter', () => saveToolFilter.mutateAsync({ name: mcp.name, regex: toolFilterRegex }),
                          { onFail: (e) => setToolFilterError(e instanceof Error ? e.message : 'The filter was not saved') })
                      }}
                      disabled={
                        saveToolFilter.isPending ||
                        toolFilterRegex === (mcp.tool_filter_regex || '')
                      }
                    >
                      {saveToolFilter.isPending ? 'Saving...' : 'Save filter'}
                    </Button>
                    <SaveState state={stateOf('tool_filter')} besideButton />
                    {toolFilterError && (
                      <span className="text-xs text-status-danger-fg">
                        {toolFilterError}
                      </span>
                    )}
                  </div>
                )}
              </div>

              {/* Infrastructure credentials */}
              {mcp.credential_type === 'infra' && mcp.credential_fields && mcp.credential_fields.length > 0 && (
                <div className={`${CARD_BORDERED} p-3`}>
                  <div className="flex items-center gap-2 mb-2">
                    <p className="text-2xs font-semibold text-tertiary uppercase tracking-wide">Credentials</p>
                    {mcp.credential_configured ? (
                      <span className="flex items-center gap-1 text-3xs text-status-success-fg">
                        <svg className="w-3 h-3" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M5 13l4 4L19 7" /></svg>
                        Configured
                      </span>
                    ) : (
                      <span className="flex items-center gap-1 text-3xs text-status-warning-fg">
                        <svg className="w-3 h-3" fill="currentColor" viewBox="0 0 8 8"><circle cx="4" cy="4" r="3" /></svg>
                        Not set
                      </span>
                    )}
                  </div>
                  <div className="space-y-2.5">
                    {mcp.credential_fields.map(f => (
                      <div key={f.key}>
                        <label className="block text-xs text-secondary mb-1">{f.label}</label>
                        <Input compact
                          type={f.input_type === 'password' ? 'password' : 'text'}
                          placeholder={mcp.credential_configured_keys?.includes(f.key) ? 'Configured' : ''}
                          value={credValues[f.key] || ''}
                          onChange={(e) => setCredValues({ ...credValues, [f.key]: e.target.value })}
                        />
                      </div>
                    ))}
                    <div className="flex items-center gap-2 pt-1">
                      <Button variant="primary" size="xs"
                        onClick={() => {
                          setCredError('')
                          void run('credentials', () => saveInfraCreds.mutateAsync({ mcpName: mcp.name, credentials: credValues }).then(() => setCredValues({})),
                            { onFail: (e) => setCredError(e instanceof Error ? e.message : 'The credentials were not saved') })
                        }}
                        disabled={saveInfraCreds.isPending || Object.values(credValues).every(v => !v)}
                      >
                        {saveInfraCreds.isPending ? 'Saving...' : 'Save'}
                      </Button>
                      <SaveState state={stateOf('credentials')} besideButton />
                      {credError && <span className="text-xs text-status-danger-fg">{credError}</span>}
                    </div>
                  </div>
                </div>
              )}

              {/* Hosted OAuth toggle — relay-routed; OtoDock's
                  client_secret stays in the relay, never in this install. */}
              {mcp.hosted?.oauth_app?.available && !ownServerOnly && (
                <div className={`${CARD_BORDERED} p-3`}>
                  <div className="flex items-center justify-between mb-1.5">
                    <div className="min-w-0">
                      <p className="inline text-2xs font-semibold text-tertiary uppercase tracking-wide">Hosted via OtoDock</p>
                      <InfoTip label="Hosted via OtoDock">Use OtoDock's verified OAuth app, with no developer setup. OtoDock keeps the app's secret and runs the sign-in.</InfoTip>
                      <SaveState state={stateOf('hosted_mode')} />
                    </div>
                    <span className={`${BADGE_BASE} ${BADGE_TONE[hostedOauthMode === 'hosted' ? 'action' : 'neutral']}`}>
                      {hostedOauthMode === 'hosted' ? 'Hosted' : 'Self-managed'}
                    </span>
                  </div>
                  <p className="text-xs text-secondary mb-3">Only your users' own account access reaches this install.</p>
                  {authConfig?.air_gapped ? (
                    <p className="text-2xs text-tertiary italic">
                      Hosted OAuth is disabled on air-gapped installs. Use self-managed credentials below.
                    </p>
                  ) : (
                    <>
                      <div role="group" aria-label="Sign-in mode" className="flex gap-2">
                        <button
                          type="button"
                          onClick={() => {
                            if (hostedOauthMode === 'hosted') return
                            void run('hosted_mode', () => setHostedMode.mutateAsync({ name: mcp.name, mode: 'hosted' }), { failTitle: 'The sign-in mode was not changed' })
                          }}
                          disabled={setHostedMode.isPending}
                          aria-pressed={hostedOauthMode === 'hosted'}
                          className={`${BUTTON_BASE} ${BUTTON_SIZE.xs} ${hostedOauthMode === 'hosted' ? SEGMENT.on : SEGMENT.off}`}
                        >
                          Use OtoDock
                        </button>
                        <button
                          type="button"
                          onClick={() => {
                            if (hostedOauthMode !== 'hosted') return
                            void run('hosted_mode', () => setHostedMode.mutateAsync({ name: mcp.name, mode: 'self_managed' }), { failTitle: 'The sign-in mode was not changed' })
                          }}
                          disabled={setHostedMode.isPending}
                          aria-pressed={hostedOauthMode !== 'hosted'}
                          className={`${BUTTON_BASE} ${BUTTON_SIZE.xs} ${hostedOauthMode !== 'hosted' ? SEGMENT.on : SEGMENT.off}`}
                        >
                          Self-managed
                        </button>
                      </div>
                      {hostedOauthMode === 'hosted' && !authConfig?.relay_available && (
                        <p className="mt-2 text-3xs text-tertiary italic">
                          Hosted via OtoDock starts working once OtoDock's hosted service is live.
                        </p>
                      )}
                      {hostedOauthMode === 'hosted' && authConfig?.relay_available && (
                        <p className="mt-2 text-3xs text-tertiary italic">
                          Connected through OtoDock. Users connect their {mcp.label} accounts with no admin setup.
                        </p>
                      )}
                    </>
                  )}
                </div>
              )}

              {/* The MCP server's own authorization server: the install
                  registers itself there as a client; a person signs in on
                  the vendor's page. The sign-in itself when the server takes
                  its own tokens only (the hosted choice and the app
                  credentials, which sign nobody in there, are hidden);
                  under the app credentials, as the fallback, when it also
                  accepts the vendor's app tokens. */}
              {ownServerOnly && registeredClientCard}

              {/* App credentials (OAuth app — admin-managed). SELF-MANAGED
                  mode only: with hosted OAuth the relay owns the client
                  ID/secret AND receives the vendor's webhook events centrally
                  (event relay) — inbound forwards verify against the
                  automatically-managed relay forward secret, so no
                  install-local app secret is needed and the panel hides. Any
                  previously-saved secrets stay stored (visible again when
                  switching to self-managed). */}
              {mcp.app_credential && !hostedOauthActive && !ownServerOnly && (() => {
                const kindEntry = appKinds.find((k) => k.kind === appKind)
                const appCredFields = kindEntry ? kindEntry.fields : (mcp.app_credential_fields || [])
                if (appCredFields.length === 0) return null
                const githubApp = isGitHubApp(appKind)
                const base = `${window.location.origin}/v1`
                return (
                <div className={`${CARD_BORDERED} p-3`}>
                  <div className="flex items-center gap-2 mb-2">
                    <p className="text-2xs font-semibold text-tertiary uppercase tracking-wide">OAuth App Credentials</p>
                    {mcp.app_credential_configured ? (
                      <span className="flex items-center gap-1 text-3xs text-status-success-fg">
                        <svg className="w-3 h-3" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M5 13l4 4L19 7" /></svg>
                        Configured
                      </span>
                    ) : (
                      <span className="flex items-center gap-1 text-3xs text-status-warning-fg">
                        <svg className="w-3 h-3" fill="currentColor" viewBox="0 0 8 8"><circle cx="4" cy="4" r="3" /></svg>
                        Not set
                      </span>
                    )}
                  </div>
                  {appKinds.length > 1 && (
                    <div className="flex items-center gap-3 mb-2" role="radiogroup" aria-label="Application kind">
                      {appKinds.map((k) => (
                        <label key={k.kind} className="flex items-center gap-1.5 text-xs text-primary cursor-pointer">
                          <input
                            type="radio"
                            name={`app-kind-${mcp.name}`}
                            checked={appKind === k.kind}
                            onChange={() => { setAppKindPick(k.kind); setAppCredValues({}) }}
                            className="accent-action"
                          />
                          {k.label}
                        </label>
                      ))}
                    </div>
                  )}
                  {githubApp ? (
                    <>
                      <p className="text-3xs text-tertiary mb-2">
                        Register a GitHub App in GitHub's Settings, Developer settings, GitHub Apps. Paste its values here. Use the three addresses below on its settings page. They are the callback URL, the setup URL and the webhook URL. Turn on &quot;Redirect on update&quot; for the setup URL.
                      </p>
                      <code className="block text-3xs bg-surface-muted px-2 py-1.5 rounded-sm mb-1 text-secondary break-all select-all">
                        {`${base}/oauth/${mcp.provider_id || 'github'}/callback`}
                      </code>
                      <code className="block text-3xs bg-surface-muted px-2 py-1.5 rounded-sm mb-1 text-secondary break-all select-all">
                        {`${base}/oauth/${mcp.provider_id || 'github'}/app/setup`}
                      </code>
                      <code className="block text-3xs bg-surface-muted px-2 py-1.5 rounded-sm mb-3 text-secondary break-all select-all">
                        {`${base}/webhooks/${mcp.provider_id || 'github'}/app`}
                      </code>
                    </>
                  ) : (
                    <>
                      <p className="text-3xs text-tertiary mb-2">
                        Register an OAuth application with {mcp.label} and paste its credentials here. Use this redirect URI in the {mcp.label} developer console:
                      </p>
                      <code className="block text-3xs bg-surface-muted px-2 py-1.5 rounded-sm mb-3 text-secondary break-all select-all">
                        {`${base}/oauth/${mcp.provider_id || 'google'}/callback`}
                      </code>
                    </>
                  )}
                  <div className="space-y-2.5">
                    {appCredFields.map(f => (
                      <div key={f.key}>
                        <label className="block text-xs text-secondary mb-1">{f.label}</label>
                        {f.input_type === INPUT_MULTILINE_SECRET ? (
                          <textarea
                            rows={5}
                            spellCheck={false}
                            autoComplete="off"
                            placeholder={mcp.app_credential_configured_keys?.includes(f.key) ? 'Configured' : ''}
                            value={appCredValues[f.key] || ''}
                            onChange={(e) => setAppCredValues({ ...appCredValues, [f.key]: e.target.value })}
                            className={`${FIELD} ${FIELD_COMPACT} ${FIELD_BORDER} font-mono`}
                          />
                        ) : (
                        <Input compact
                          type={f.input_type === 'password' ? 'password' : 'text'}
                          placeholder={mcp.app_credential_configured_keys?.includes(f.key) ? 'Configured' : ''}
                          value={appCredValues[f.key] || ''}
                          onChange={(e) => setAppCredValues({ ...appCredValues, [f.key]: e.target.value })}
                        />
                        )}
                        {f.help && (
                          <p className="mt-1 text-3xs text-tertiary italic">{f.help}</p>
                        )}
                      </div>
                    ))}
                    <div className="flex items-center gap-2 pt-1">
                      <Button variant="primary" size="xs"
                        onClick={() => {
                          setAppCredError('')
                          void run('app_credentials', () => saveInfraCreds.mutateAsync({
                            mcpName: mcp.app_credential!,
                            // The kind rides with the fields so the bundle records it.
                            credentials: mcp.app_kinds ? { ...appCredValues, [mcp.app_kinds.key]: appKind } : appCredValues,
                          }).then(() => setAppCredValues({})), {
                            onFail: (e) => setAppCredError(e instanceof Error ? e.message : 'The credentials were not saved'),
                          })
                        }}
                        disabled={saveInfraCreds.isPending || Object.values(appCredValues).every(v => !v)}
                      >
                        {saveInfraCreds.isPending ? 'Saving...' : 'Save'}
                      </Button>
                      <SaveState state={stateOf('app_credentials')} besideButton />
                      {appCredError && <span className="text-xs text-status-danger-fg">{appCredError}</span>}
                    </div>
                  </div>
                </div>
                )
              })()}

              {/* The fallback sign-in of a server that also accepts the
                  vendor's app tokens: below the app credentials it stands in
                  for. */}
              {authServer?.accepts_app_tokens && registeredClientCard}

            </div>
          </div>

          {/* Instance management (SSH hosts, Prometheus servers, etc.) */}
          {mcp.instances && (
            <div className="border-t border-subtle p-4">
              <InstanceManagerWrapper mcpName={mcp.name} apiKeyRelay={mcp.hosted?.api_key_relay} />
            </div>
          )}
        </div>
      )}
    </div>
  )
}
