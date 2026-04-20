import { useState } from 'react'
import { useEnableMcp, useDisableMcp, useSetMcpConfig, useSetMcpToolFilter, useSetHostedServiceMode, useSetNetworkAccess, useDockerAction, useDeleteMcp, useUpdateMcp, McpServer, McpUpdateInfo } from '../../api/mcps'
import { useAgents } from '../../api/agents'
import { useSetInfraCredentials } from '../../api/credentials'
import McpInstanceManager, { ApiKeyRelayInfo } from '../../components/admin/McpInstanceManager'
import { useAuth } from '../../contexts/AuthContext'

const DOCKER_STATUS: Record<string, { label: string; color: string }> = {
  running: { label: 'Running', color: 'text-green-600 dark:text-green-400' },
  // Container is up but failing its healthcheck (e.g. a wedged camoufox) — amber,
  // not green. The proxy never auto-restarts Docker MCPs, so it's operator-visible.
  unhealthy: { label: 'Unhealthy', color: 'text-amber-500 dark:text-amber-400' },
  starting: { label: 'Starting…', color: 'text-amber-500 dark:text-amber-400' },
  stopped: { label: 'Stopped', color: 'text-red-500 dark:text-red-400' },
  not_found: { label: 'Not Found', color: 'text-gray-400' },
  error: { label: 'Error', color: 'text-red-500' },
  unknown: { label: 'Unknown', color: 'text-gray-400' },
  not_checked: { label: '...', color: 'text-gray-400' },
}

function InstanceManagerWrapper({ mcpName, apiKeyRelay }: { mcpName: string; apiKeyRelay?: ApiKeyRelayInfo }) {
  const { data: agentList } = useAgents({ all: true })
  const agents = agentList?.map(a => a.name) || []
  const { authConfig } = useAuth()
  return <McpInstanceManager mcpName={mcpName} agents={agents} apiKeyRelay={apiKeyRelay}
    airGapped={!!authConfig?.air_gapped} relayAvailable={!!authConfig?.relay_available} />
}

export function McpRow({ mcp, updateInfo }: { mcp: McpServer; updateInfo?: McpUpdateInfo }) {
  const [expanded, setExpanded] = useState(false)
  const [configValues, setConfigValues] = useState<Record<string, string>>(mcp.config_values || {})
  const [configSaved, setConfigSaved] = useState(false)
  const [credValues, setCredValues] = useState<Record<string, string>>({})
  const [credSaved, setCredSaved] = useState(false)
  const [credError, setCredError] = useState('')
  const [appCredValues, setAppCredValues] = useState<Record<string, string>>({})
  const [appCredSaved, setAppCredSaved] = useState(false)
  const [appCredError, setAppCredError] = useState('')
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
  const [updateLog, setUpdateLog] = useState('')
  const [toolFilterRegex, setToolFilterRegex] = useState(mcp.tool_filter_regex || '')
  const [toolFilterSaved, setToolFilterSaved] = useState(false)
  const [toolFilterError, setToolFilterError] = useState('')

  // The kill-switchable core MCPs (meetings/delegation) stay toggleable —
  // the server's can_disable is the authority, not the category.
  const lockedOn = !mcp.can_disable
  // docker_managed === false: the container is an operator-owned sibling of
  // the platform's own compose stack (core file-tools on containerized
  // installs) — the proxy can't report or drive it, so the status pill and
  // start/stop/restart controls are hidden rather than showing "Not Found".
  const isDocker = mcp.runtime === 'docker' && mcp.docker_managed !== false
  const hasConfig = mcp.config_fields.length > 0
  const hasSkills = mcp.skills.length > 0
  const dockerInfo = isDocker ? DOCKER_STATUS[mcp.docker_status || 'not_checked'] : null

  const handleToggle = () => {
    if (lockedOn) return
    if (mcp.enabled) {
      disable.mutate(mcp.name)
    } else {
      enable.mutate(mcp.name, {
        onSuccess: (result) => {
          // Surface Docker container start failures — previously silent.
          // ``docker_status`` is null for non-Docker MCPs; "started" on
          // success; "failed" with ``docker_error`` when the compose call
          // exited non-zero (Docker daemon down, image build failed, port
          // conflict, etc.).
          if (result?.docker_status === 'failed') {
            const msg = result.docker_error || 'Docker container failed to start.'
            window.alert(
              `MCP "${mcp.name}" was enabled in the platform but the Docker ` +
              `container did NOT start.\n\n${msg}\n\n` +
              `Fix the issue, then click the "Start" button in the Docker ` +
              `controls below to retry.`,
            )
          }
        },
      })
    }
  }

  const handleSaveConfig = () => {
    saveConfig.mutate({ name: mcp.name, values: configValues }, {
      onSuccess: () => { setConfigSaved(true); setTimeout(() => setConfigSaved(false), 2000) },
    })
  }

  return (
    <div className="border border-p-border-light rounded-xl bg-white dark:bg-p-surface overflow-hidden">
      {/* Header row */}
      <div
        className="flex items-center gap-3 px-4 py-3 cursor-pointer hover:bg-p-surface-hover/50 transition-colors"
        onClick={() => setExpanded(!expanded)}
      >
        {/* Enable/disable toggle */}
        <button
          onClick={(e) => { e.stopPropagation(); handleToggle() }}
          disabled={lockedOn || enable.isPending || disable.isPending}
          className={`w-10 h-[22px] rounded-full relative transition-colors shrink-0 ${
            mcp.enabled
              ? 'bg-green-500'
              : 'bg-gray-300 dark:bg-gray-600'
          } ${lockedOn ? 'opacity-60 cursor-not-allowed' : ''}`}
          title={lockedOn ? 'Core MCP — always enabled' : mcp.enabled ? 'Disable' : 'Enable'}
        >
          <span className={`absolute top-[3px] left-[3px] w-4 h-4 rounded-full bg-white shadow-xs transition-transform ${
            mcp.enabled ? 'translate-x-[18px]' : 'translate-x-0'
          }`} />
        </button>

        <div className="flex-1 min-w-0">
          <div className="flex items-center gap-2 flex-wrap">
            <span className="font-medium text-sm text-p-text">{mcp.label}</span>
            {mcp.assignment_mode === 'explicit' && (
              <span className="text-[10px] px-1.5 py-0.5 rounded-md font-medium bg-amber-100 dark:bg-amber-900/30 text-amber-700 dark:text-amber-400">
                explicit assignment
              </span>
            )}
            {updateInfo && (
              <span className="text-[10px] px-1.5 py-0.5 rounded-md font-medium bg-brand/10 dark:bg-brand/20 text-brand animate-pulse">
                {updateInfo.reason === 'manifest' ? 'integration update' : `${updateInfo.latest} available`}
              </span>
            )}
          </div>
        </div>

        {/* Docker status */}
        {dockerInfo && (
          <span className={`text-xs font-medium ${dockerInfo.color} shrink-0`}>
            {dockerInfo.label}
          </span>
        )}

        {/* Expand chevron */}
        <svg className={`w-4 h-4 text-p-text-light transition-transform shrink-0 ${expanded ? 'rotate-180' : ''}`} fill="none" stroke="currentColor" viewBox="0 0 24 24">
          <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M19 9l-7 7-7-7" />
        </svg>
      </div>

      {/* Expanded detail */}
      {expanded && (
        <div className="border-t border-p-border-light bg-gray-50/50 dark:bg-gray-900/30">
          {/* Two-column grid on desktop, single on mobile */}
          <div className="grid grid-cols-1 md:grid-cols-2 gap-0 md:gap-4 p-4">
            {/* LEFT: Info */}
            <div className="space-y-3 mb-4 md:mb-0">
              {/* Description & meta */}
              <div className="rounded-lg border border-p-border-light/60 bg-white dark:bg-gray-800/50 p-3">
                <p className="text-sm text-p-text-secondary leading-relaxed">{mcp.description}</p>
                <div className="mt-2 flex flex-wrap gap-x-4 gap-y-1 text-[11px] text-p-text-light">
                  <span>v{mcp.version || 'latest'}</span>
                  {mcp.source && <span className="font-mono truncate max-w-48">{mcp.source}</span>}
                </div>
              </div>

              {/* Agents */}
              {mcp.agents.length > 0 && (
                <div className="rounded-lg border border-p-border-light/60 bg-white dark:bg-gray-800/50 p-3">
                  <p className="text-[11px] font-semibold text-p-text-light uppercase tracking-wide mb-2">Enabled for</p>
                  <div className="flex gap-1.5 flex-wrap">
                    {mcp.agents.map(a => (
                      <span key={a} className="text-xs px-2 py-0.5 rounded-md bg-brand/5 dark:bg-brand/10 text-brand border border-brand/10">{a}</span>
                    ))}
                  </div>
                </div>
              )}

              {/* Skills */}
              {hasSkills && (
                <div className="rounded-lg border border-p-border-light/60 bg-white dark:bg-gray-800/50 p-3">
                  <p className="text-[11px] font-semibold text-p-text-light uppercase tracking-wide mb-2">Skills</p>
                  <div className="space-y-1">
                    {mcp.skills.map(s => (
                      <p key={s.id} className="text-xs text-p-text-secondary">
                        <span className="font-mono text-brand">{s.id}</span>
                        {s.description && <span className="ml-1.5 text-p-text-light">— {s.description}</span>}
                      </p>
                    ))}
                  </div>
                </div>
              )}

              {/* Per-user credentials note */}
              {mcp.credential_type === 'per_user' && (
                <div className="rounded-lg border border-amber-200/60 dark:border-amber-800/40 bg-amber-50/50 dark:bg-amber-900/10 p-3">
                  <p className="text-xs text-amber-700 dark:text-amber-400">Per-user credentials — users configure these in their personal settings.</p>
                </div>
              )}

              {/* Update available */}
              {updateInfo && (
                <div className="rounded-lg border border-brand/30 dark:border-brand/20 bg-brand/5 dark:bg-brand/10 p-3">
                  <div className="flex items-center justify-between mb-2">
                    <div>
                      <p className="text-xs font-medium text-brand">Update Available</p>
                      <p className="text-[11px] text-p-text-secondary">
                        {updateInfo.reason === 'manifest'
                          ? 'Integration update (manifest changed)'
                          : <>{updateInfo.current} → {updateInfo.latest}</>}
                        <span className="text-p-text-light ml-1">({updateInfo.registry})</span>
                      </p>
                    </div>
                    <button
                      onClick={() => {
                        setUpdateLog('')
                        updateMcp.mutate(mcp.name, {
                          onSuccess: (data: any) => setUpdateLog(data.install_log || 'Updated successfully'),
                          onError: (e: Error) => setUpdateLog(`Error: ${e.message}`),
                        })
                      }}
                      disabled={updateMcp.isPending}
                      className="text-xs px-3 py-1.5 rounded-md bg-brand text-white hover:bg-brand-hover disabled:opacity-40 transition-colors"
                    >
                      {updateMcp.isPending ? 'Updating...' : 'Update'}
                    </button>
                  </div>
                  {updateLog && (
                    <details open={updateLog.startsWith('Error')}>
                      <summary className="text-[11px] text-p-text-light cursor-pointer">Install log</summary>
                      <pre className="mt-1 text-[11px] text-p-text-light bg-gray-100 dark:bg-gray-900 rounded-sm p-2 overflow-x-auto max-h-32 overflow-y-auto whitespace-pre-wrap">{updateLog}</pre>
                    </details>
                  )}
                </div>
              )}

              {/* Delete MCP (community only — custom MCPs ship with the platform and aren't re-installable) */}
              {mcp.category === 'community' && (
                <div className="rounded-lg border border-red-200/60 dark:border-red-800/40 bg-red-50/30 dark:bg-red-900/10 p-3">
                  <div className="flex items-center justify-between">
                    <div>
                      <p className="text-xs font-medium text-red-600 dark:text-red-400">Delete MCP</p>
                      <p className="text-[11px] text-p-text-light">Removes folder, config, credentials, and all agent assignments. Reinstall later via Browse Community MCPs.</p>
                    </div>
                    <button
                      onClick={() => {
                        if (window.confirm(`Delete "${mcp.label}"? This will remove the MCP folder, all config, credentials, and agent assignments. This cannot be undone.`)) {
                          deleteMcp.mutate(mcp.name)
                        }
                      }}
                      disabled={deleteMcp.isPending}
                      className="text-xs px-3 py-1.5 rounded-md border border-red-300 dark:border-red-700 text-red-600 dark:text-red-400 hover:bg-red-100 dark:hover:bg-red-900/30 transition-colors disabled:opacity-40"
                    >
                      {deleteMcp.isPending ? 'Deleting...' : 'Delete'}
                    </button>
                  </div>
                </div>
              )}
            </div>

            {/* RIGHT: Actionable sections */}
            <div className="space-y-3">
              {/* Docker controls */}
              {isDocker && (
                <div className="rounded-lg border border-p-border-light/60 bg-white dark:bg-gray-800/50 p-3">
                  <p className="text-[11px] font-semibold text-p-text-light uppercase tracking-wide mb-2">Docker</p>
                  <div className="flex items-center gap-2">
                    <button
                      onClick={() => dockerAction.mutate({ name: mcp.name, action: 'start' })}
                      disabled={dockerAction.isPending || mcp.docker_status === 'running'}
                      className="text-xs px-3 py-1.5 rounded-md bg-green-600 text-white hover:bg-green-700 disabled:opacity-40 transition-colors"
                    >
                      Start
                    </button>
                    <button
                      onClick={() => dockerAction.mutate({ name: mcp.name, action: 'stop' })}
                      disabled={dockerAction.isPending || mcp.docker_status === 'stopped'}
                      className="text-xs px-3 py-1.5 rounded-md bg-red-600 text-white hover:bg-red-700 disabled:opacity-40 transition-colors"
                    >
                      Stop
                    </button>
                    <button
                      onClick={() => dockerAction.mutate({ name: mcp.name, action: 'restart' })}
                      disabled={dockerAction.isPending}
                      className="text-xs px-3 py-1.5 rounded-md bg-amber-600 text-white hover:bg-amber-700 disabled:opacity-40 transition-colors"
                    >
                      Restart
                    </button>
                  </div>
                </div>
              )}

              {/* Config fields */}
              {hasConfig && (
                <div className="rounded-lg border border-p-border-light/60 bg-white dark:bg-gray-800/50 p-3">
                  <p className="text-[11px] font-semibold text-p-text-light uppercase tracking-wide mb-2">Configuration</p>
                  <div className="space-y-2.5">
                    {mcp.config_fields.map(f => (
                      <div key={f.key}>
                        <label className="block text-xs text-p-text-secondary mb-1">{f.label}</label>
                        <input
                          type={f.input_type === 'password' ? 'password' : 'text'}
                          value={configValues[f.key] ?? f.default ?? ''}
                          onChange={(e) => setConfigValues({ ...configValues, [f.key]: e.target.value })}
                          placeholder={f.default || ''}
                          className="w-full text-xs px-2.5 py-2 rounded-md border border-p-border-light bg-p-bg dark:bg-gray-900 text-p-text focus:outline-hidden focus:ring-1 focus:ring-brand/30"
                        />
                      </div>
                    ))}
                    <div className="flex items-center gap-2 pt-1">
                      <button
                        onClick={handleSaveConfig}
                        disabled={saveConfig.isPending}
                        className="text-xs px-3 py-1.5 rounded-md bg-brand text-white hover:bg-brand-hover disabled:opacity-40 transition-colors"
                      >
                        {saveConfig.isPending ? 'Saving...' : 'Save'}
                      </button>
                      {configSaved && <span className="text-xs text-green-600 dark:text-green-400">Saved</span>}
                    </div>
                  </div>
                </div>
              )}

              {/* Internal-network access (homelab MCPs declaring network_targets).
                  When on, the agent sandbox carves egress to exactly this MCP's
                  configured target host — nothing else on the LAN. Unavailable
                  on hosted OtoDock (no operator network). */}
              {mcp.has_network_targets && (
                <div className="rounded-lg border border-p-border-light/60 bg-white dark:bg-gray-800/50 p-3">
                  <p className="text-[11px] font-semibold text-p-text-light uppercase tracking-wide mb-2">Internal network access</p>
                  {authConfig?.cloud ? (
                    <p className="text-xs text-p-text-secondary leading-snug">
                      Unavailable on hosted OtoDock — this MCP can only reach
                      publicly-routable targets.
                    </p>
                  ) : (
                    <>
                      <label className="flex items-center gap-2 cursor-pointer">
                        <input
                          type="checkbox"
                          checked={mcp.network_access ?? true}
                          disabled={setNetworkAccess.isPending}
                          onChange={(e) => setNetworkAccess.mutate({ name: mcp.name, enabled: e.target.checked })}
                          className="accent-brand"
                        />
                        <span className="text-xs text-p-text">
                          Allow this MCP to reach its configured endpoint on your network
                        </span>
                      </label>
                      <p className="text-[11px] text-p-text-secondary leading-snug mt-1.5">
                        The agent's sandbox is network-isolated; this carves a hole to
                        exactly the host you configured above and nothing else.
                      </p>
                    </>
                  )}
                </div>
              )}

              {/* Tool Filter. Generic per-MCP
                  runtime tool restriction — rendered for every MCP, but
                  enabled only when manifest declares `tool_filter`. */}
              <div className="rounded-lg border border-p-border-light/60 bg-white dark:bg-gray-800/50 p-3">
                <div className="flex items-center justify-between mb-1.5">
                  <p className="text-[11px] font-semibold text-p-text-light uppercase tracking-wide">
                    Tool Filter
                  </p>
                  {!mcp.tool_filter_supported && (
                    <span
                      className="text-[10px] text-p-text-light italic"
                      title="This MCP does not declare tool_filter in its manifest; runtime filtering is unavailable."
                    >
                      Not supported by this MCP
                    </span>
                  )}
                </div>
                <p className="text-xs text-p-text-secondary mb-2">
                  {mcp.tool_filter_supported
                    ? 'Limit which tools this MCP gives agents. Leave empty to allow every tool. Otherwise enter a pattern matching the tool names you want to keep — e.g. mail keeps any tool whose name contains "mail", or ^(mail|calendar)_ keeps only the mail and calendar tools.'
                    : 'This MCP exposes its full tool surface — no runtime restriction supported.'}
                </p>
                <input
                  type="text"
                  value={toolFilterRegex}
                  onChange={(e) => setToolFilterRegex(e.target.value)}
                  disabled={!mcp.tool_filter_supported}
                  placeholder={
                    mcp.tool_filter_supported
                      ? "^(mail|calendar)_.*"
                      : '(not supported)'
                  }
                  className="w-full text-xs px-2.5 py-2 rounded-md border border-p-border-light bg-p-bg dark:bg-gray-900 text-p-text font-mono focus:outline-hidden focus:ring-1 focus:ring-brand/30 disabled:opacity-50 disabled:cursor-not-allowed"
                />
                {mcp.tool_filter_supported && (
                  <div className="flex items-center gap-2 pt-2">
                    <button
                      onClick={() => {
                        setToolFilterError('')
                        saveToolFilter.mutate(
                          { name: mcp.name, regex: toolFilterRegex },
                          {
                            onSuccess: () => {
                              setToolFilterSaved(true)
                              setTimeout(() => setToolFilterSaved(false), 2000)
                            },
                            onError: (e: Error) =>
                              setToolFilterError(e.message),
                          },
                        )
                      }}
                      disabled={
                        saveToolFilter.isPending ||
                        toolFilterRegex === (mcp.tool_filter_regex || '')
                      }
                      className="text-xs px-3 py-1.5 rounded-md bg-brand text-white hover:bg-brand-hover disabled:opacity-40 transition-colors"
                    >
                      {saveToolFilter.isPending ? 'Saving...' : 'Save filter'}
                    </button>
                    {toolFilterSaved && (
                      <span className="text-xs text-green-600 dark:text-green-400">
                        Saved — Docker container restarting if applicable.
                      </span>
                    )}
                    {toolFilterError && (
                      <span className="text-xs text-red-500">
                        {toolFilterError}
                      </span>
                    )}
                  </div>
                )}
              </div>

              {/* Infrastructure credentials */}
              {mcp.credential_type === 'infra' && mcp.credential_fields && mcp.credential_fields.length > 0 && (
                <div className="rounded-lg border border-p-border-light/60 bg-white dark:bg-gray-800/50 p-3">
                  <div className="flex items-center gap-2 mb-2">
                    <p className="text-[11px] font-semibold text-p-text-light uppercase tracking-wide">Credentials</p>
                    {mcp.credential_configured ? (
                      <span className="flex items-center gap-1 text-[10px] text-green-600 dark:text-green-400">
                        <svg className="w-3 h-3" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M5 13l4 4L19 7" /></svg>
                        Configured
                      </span>
                    ) : (
                      <span className="flex items-center gap-1 text-[10px] text-amber-600 dark:text-amber-400">
                        <svg className="w-3 h-3" fill="currentColor" viewBox="0 0 8 8"><circle cx="4" cy="4" r="3" /></svg>
                        Not set
                      </span>
                    )}
                  </div>
                  <div className="space-y-2.5">
                    {mcp.credential_fields.map(f => (
                      <div key={f.key}>
                        <label className="block text-xs text-p-text-secondary mb-1">{f.label}</label>
                        <input
                          type={f.input_type === 'password' ? 'password' : 'text'}
                          placeholder={mcp.credential_configured_keys?.includes(f.key) ? '(configured)' : ''}
                          value={credValues[f.key] || ''}
                          onChange={(e) => setCredValues({ ...credValues, [f.key]: e.target.value })}
                          className="w-full text-xs px-2.5 py-2 rounded-md border border-p-border-light bg-p-bg dark:bg-gray-900 text-p-text focus:outline-hidden focus:ring-1 focus:ring-brand/30"
                        />
                      </div>
                    ))}
                    <div className="flex items-center gap-2 pt-1">
                      <button
                        onClick={() => {
                          setCredError('')
                          saveInfraCreds.mutate(
                            { mcpName: mcp.name, credentials: credValues },
                            {
                              onSuccess: () => { setCredSaved(true); setCredValues({}); setTimeout(() => setCredSaved(false), 2000) },
                              onError: (e: Error) => setCredError(e.message),
                            },
                          )
                        }}
                        disabled={saveInfraCreds.isPending || Object.values(credValues).every(v => !v)}
                        className="text-xs px-3 py-1.5 rounded-md bg-brand text-white hover:bg-brand-hover disabled:opacity-40 transition-colors"
                      >
                        {saveInfraCreds.isPending ? 'Saving...' : 'Save'}
                      </button>
                      {credSaved && <span className="text-xs text-green-600 dark:text-green-400">Saved</span>}
                      {credError && <span className="text-xs text-red-500">{credError}</span>}
                    </div>
                  </div>
                </div>
              )}

              {/* Hosted OAuth toggle — relay-routed; OtoDock's
                  client_secret stays in the relay, never in this install. */}
              {mcp.hosted?.oauth_app?.available && (
                <div className="rounded-lg border border-p-border-light/60 bg-white dark:bg-gray-800/50 p-3">
                  <div className="flex items-center justify-between mb-1.5">
                    <p className="text-[11px] font-semibold text-p-text-light uppercase tracking-wide">
                      Hosted via OtoDock
                    </p>
                    <span className={`text-[10px] px-1.5 py-0.5 rounded-md font-medium ${
                      hostedOauthMode === 'hosted'
                        ? 'bg-brand/10 text-brand'
                        : 'bg-gray-100 dark:bg-gray-800 text-p-text-light'
                    }`}>
                      {hostedOauthMode === 'hosted' ? 'Hosted' : 'Self-managed'}
                    </span>
                  </div>
                  <p className="text-xs text-p-text-secondary mb-3">
                    Use OtoDock's verified OAuth app — no developer setup. The relay
                    holds the client secret and brokers the handshake; only your
                    users' own tokens reach this install.
                  </p>
                  {authConfig?.air_gapped ? (
                    <p className="text-[11px] text-p-text-light italic">
                      Hosted OAuth is disabled on air-gapped installs. Use self-managed credentials below.
                    </p>
                  ) : (
                    <>
                      <div className="flex gap-2">
                        <button
                          onClick={() => setHostedMode.mutate({ name: mcp.name, mode: 'hosted' })}
                          disabled={setHostedMode.isPending || hostedOauthMode === 'hosted'}
                          className={`text-xs px-3 py-1.5 rounded-md transition-colors ${
                            hostedOauthMode === 'hosted'
                              ? 'bg-brand text-white'
                              : 'border border-p-border-light text-p-text-secondary hover:bg-gray-50 dark:hover:bg-gray-800'
                          } disabled:opacity-40`}
                        >
                          Use OtoDock
                        </button>
                        <button
                          onClick={() => setHostedMode.mutate({ name: mcp.name, mode: 'self_managed' })}
                          disabled={setHostedMode.isPending || hostedOauthMode !== 'hosted'}
                          className={`text-xs px-3 py-1.5 rounded-md transition-colors ${
                            hostedOauthMode !== 'hosted'
                              ? 'bg-brand text-white'
                              : 'border border-p-border-light text-p-text-secondary hover:bg-gray-50 dark:hover:bg-gray-800'
                          } disabled:opacity-40`}
                        >
                          Self-managed
                        </button>
                      </div>
                      {hostedOauthMode === 'hosted' && !authConfig?.relay_available && (
                        <p className="mt-2 text-[10px] text-p-text-light italic">
                          Hosted via OtoDock — activates when the relay is live.
                        </p>
                      )}
                      {hostedOauthMode === 'hosted' && authConfig?.relay_available && (
                        <p className="mt-2 text-[10px] text-p-text-light italic">
                          Connected via the OtoDock relay — users connect their {mcp.label} accounts with no admin setup.
                        </p>
                      )}
                    </>
                  )}
                </div>
              )}

              {/* App credentials (OAuth app — admin-managed). SELF-MANAGED
                  mode only: with hosted OAuth the relay owns the client
                  ID/secret AND receives the vendor's webhook events centrally
                  (event relay) — inbound forwards verify against the
                  automatically-managed relay forward secret, so no
                  install-local app secret is needed and the panel hides. Any
                  previously-saved secrets stay stored (visible again when
                  switching to self-managed). */}
              {mcp.app_credential && !hostedOauthActive && (() => {
                const appCredFields = mcp.app_credential_fields || []
                if (appCredFields.length === 0) return null
                return (
                <div className="rounded-lg border border-p-border-light/60 bg-white dark:bg-gray-800/50 p-3">
                  <div className="flex items-center gap-2 mb-2">
                    <p className="text-[11px] font-semibold text-p-text-light uppercase tracking-wide">OAuth App Credentials</p>
                    {mcp.app_credential_configured ? (
                      <span className="flex items-center gap-1 text-[10px] text-green-600 dark:text-green-400">
                        <svg className="w-3 h-3" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M5 13l4 4L19 7" /></svg>
                        Configured
                      </span>
                    ) : (
                      <span className="flex items-center gap-1 text-[10px] text-amber-600 dark:text-amber-400">
                        <svg className="w-3 h-3" fill="currentColor" viewBox="0 0 8 8"><circle cx="4" cy="4" r="3" /></svg>
                        Not set
                      </span>
                    )}
                  </div>
                  <p className="text-[10px] text-p-text-light mb-2">
                    Register an OAuth application with {mcp.label} and paste its credentials here. Use this redirect URI in the {mcp.label} developer console:
                  </p>
                  <code className="block text-[10px] bg-gray-100 dark:bg-gray-900 px-2 py-1.5 rounded-sm mb-3 text-p-text-secondary break-all select-all">
                    {`${window.location.origin}/v1/oauth/${mcp.provider_id || 'google'}/callback`}
                  </code>
                  <div className="space-y-2.5">
                    {appCredFields.map(f => (
                      <div key={f.key}>
                        <label className="block text-xs text-p-text-secondary mb-1">{f.label}</label>
                        <input
                          type={f.input_type === 'password' ? 'password' : 'text'}
                          placeholder={mcp.app_credential_configured_keys?.includes(f.key) ? '(configured)' : ''}
                          value={appCredValues[f.key] || ''}
                          onChange={(e) => setAppCredValues({ ...appCredValues, [f.key]: e.target.value })}
                          className="w-full text-xs px-2.5 py-2 rounded-md border border-p-border-light bg-p-bg dark:bg-gray-900 text-p-text focus:outline-hidden focus:ring-1 focus:ring-brand/30"
                        />
                        {f.help && (
                          <p className="mt-1 text-[10px] text-p-text-light italic">{f.help}</p>
                        )}
                      </div>
                    ))}
                    <div className="flex items-center gap-2 pt-1">
                      <button
                        onClick={() => {
                          setAppCredError('')
                          saveInfraCreds.mutate(
                            { mcpName: mcp.app_credential!, credentials: appCredValues },
                            {
                              onSuccess: () => { setAppCredSaved(true); setAppCredValues({}); setTimeout(() => setAppCredSaved(false), 2000) },
                              onError: (e: Error) => setAppCredError(e.message),
                            },
                          )
                        }}
                        disabled={saveInfraCreds.isPending || Object.values(appCredValues).every(v => !v)}
                        className="text-xs px-3 py-1.5 rounded-md bg-brand text-white hover:bg-brand-hover disabled:opacity-40 transition-colors"
                      >
                        {saveInfraCreds.isPending ? 'Saving...' : 'Save'}
                      </button>
                      {appCredSaved && <span className="text-xs text-green-600 dark:text-green-400">Saved</span>}
                      {appCredError && <span className="text-xs text-red-500">{appCredError}</span>}
                    </div>
                  </div>
                </div>
                )
              })()}

            </div>
          </div>

          {/* Instance management (SSH hosts, Prometheus servers, etc.) */}
          {mcp.instances && (
            <div className="border-t border-p-border-light/60 p-4">
              <InstanceManagerWrapper mcpName={mcp.name} apiKeyRelay={mcp.hosted?.api_key_relay} />
            </div>
          )}
        </div>
      )}
    </div>
  )
}
