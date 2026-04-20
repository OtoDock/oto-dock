/**
 * PhoneServerPill — one expandable card per telephony server.
 *
 * Manages the server row + AMI connection (host/port/user/secret), the adapter
 * bootstrap handshake (snippet → Verify), and the per-server health badge. For
 * FreePBX, BootstrapSection also shows the one-time admin checklist.
 */

import { useState } from 'react'
import {
  type PhoneServer,
  useUpdatePhoneServer,
  useDeletePhoneServer,
  useSetDefaultPhoneServer,
  useSetPhoneServerAmiSecret,
  useDeletePhoneServerAmiSecret,
  useServerBootstrap,
  useVerifyServerBootstrap,
  useApplyServerBootstrap,
  useCheckServerHealth,
} from '../../api/phone'
import { Badge, SavedBadge } from '../ui/SettingsControls'
import { CopyButton } from '../CopyButton'

const BOOTSTRAP_VARIANT: Record<PhoneServer['bootstrap_status'], 'default' | 'green' | 'amber' | 'red'> = {
  pending: 'default',
  snippet_provided: 'amber',
  verified: 'green',
  failed: 'red',
  drift: 'amber',
}

const HEALTH_VARIANT: Record<string, 'default' | 'green' | 'red'> = {
  healthy: 'green',
  unhealthy: 'red',
  unknown: 'default',
}

function cfg(server: PhoneServer, key: string): string {
  const v = server.config[key]
  return v === undefined || v === null ? '' : String(v)
}

/** The one-time admin steps the AMI adapters intentionally can't automate (no
 * SSH/CLI). Routing each inbound number via a Custom Destination makes
 * ${EXTEN}=<number> uniformly, which the bridge's AstDB lookup relies on.
 * The AMI user + dialplan snippets below the checklist are generated with the
 * credentials/endpoints pre-wired, so the admin only pastes. */
function FreePBXChecklist() {
  return (
    <div className="p-2.5 border border-p-border-light rounded-lg bg-gray-50 dark:bg-gray-800/40 space-y-1.5">
      <p className="text-xs font-semibold text-p-text-secondary">One-time FreePBX setup</p>
      <ol className="list-decimal list-inside text-xs text-p-text-light space-y-1">
        <li>Paste the <span className="font-medium">bridge dialplan</span> below into <span className="font-mono">extensions_custom.conf</span> (Admin → Config Edit).</li>
        <li>Paste the <span className="font-medium">AMI user</span> below into <span className="font-mono">manager_custom.conf</span>, then <span className="font-medium">Apply Config</span>. Its credentials are already configured on this server — nothing to type back.</li>
        <li>Route each inbound AI number to the bridge (once per number): create a <span className="font-medium">Custom Destination</span> targeting <span className="font-mono">oto-audiosocket-bridge,&lt;number&gt;,1</span>, then send the number to it — via its <span className="font-medium">Inbound Route</span> (an external number / DID) or a <span className="font-medium">Misc Application</span> (an internal extension) → Apply Config. Outbound-only numbers need no routing.</li>
        <li>Allow traffic: PBX → OtoDock <span className="font-mono">TCP 9092-9093</span>, OtoDock → PBX <span className="font-mono">TCP 5038</span> — in your network firewall AND FreePBX's own firewall (Connectivity → Firewall), if enabled.</li>
      </ol>
      <p className="text-xs italic text-p-text-light">Verify checks the AMI connection only — a test call confirms the dialplan + routing.</p>
    </div>
  )
}

/** Plain-Asterisk variant: same generated snippets, no FreePBX GUI — short
 * pointers only; per-route AstDB commands are shown when a route is added. */
function ManualAsteriskChecklist() {
  return (
    <div className="p-2.5 border border-p-border-light rounded-lg bg-gray-50 dark:bg-gray-800/40 space-y-1.5">
      <p className="text-xs font-semibold text-p-text-secondary">One-time Asterisk setup</p>
      <ol className="list-decimal list-inside text-xs text-p-text-light space-y-1">
        <li>Paste the <span className="font-medium">bridge dialplan</span> below into your dialplan (e.g. <span className="font-mono">extensions_custom.conf</span>), then <span className="font-mono">dialplan reload</span>.</li>
        <li>Paste the <span className="font-medium">AMI user</span> below into <span className="font-mono">manager.conf</span>, then <span className="font-mono">manager reload</span>. Its credentials are already configured on this server.</li>
        <li>Point each inbound number at <span className="font-mono">oto-audiosocket-bridge,&lt;number&gt;,1</span> — the per-route <span className="font-mono">database put</span> command is shown when you add a route.</li>
        <li>Allow traffic: PBX → OtoDock <span className="font-mono">TCP 9092-9093</span>, OtoDock → PBX <span className="font-mono">TCP 5038</span>.</li>
      </ol>
      <p className="text-xs italic text-p-text-light">Verify checks the AMI connection only — a test call confirms the dialplan + routing.</p>
    </div>
  )
}

/** Bootstrap handshake + health UI for one server (replaces the earlier placeholder). */
function BootstrapSection({ server }: { server: PhoneServer }) {
  const { data: boot, isLoading } = useServerBootstrap(server.id, true)
  const verify = useVerifyServerBootstrap()
  const apply = useApplyServerBootstrap()
  const health = useCheckServerHealth()
  const [showLog, setShowLog] = useState(false)
  const [showSftp, setShowSftp] = useState(false)
  const [sftp, setSftp] = useState({ host: '', port: '22', username: '', password: '' })

  if (isLoading || !boot) return <p className="text-xs text-p-text-light">Loading bootstrap…</p>
  if (!boot.requires_bootstrap) {
    return <p className="text-xs text-p-text-light">This provider needs no manual bootstrap — it's usable once credentials are valid.</p>
  }

  const verified = server.bootstrap_status === 'verified'

  return (
    <div className="space-y-2">
      <div className="flex items-center justify-between">
        <p className="text-xs font-semibold text-p-text-secondary uppercase tracking-wider">Bootstrap</p>
        <div className="flex items-center gap-2">
          <button onClick={() => health.mutate(server.id)} className="text-xs text-p-text-secondary hover:text-p-text">Re-check health</button>
          {!verified && (
            <button
              onClick={() => verify.mutate(server.id, { onError: (e) => alert((e as Error).message) })}
              disabled={verify.isPending}
              className="px-3 py-1 text-xs font-medium rounded-lg bg-brand text-white hover:bg-brand-hover disabled:opacity-40"
            >
              {verify.isPending ? 'Verifying…' : 'Verify'}
            </button>
          )}
        </div>
      </div>

      {verified ? (
        <p className="text-xs text-green-600 dark:text-green-400">✓ Verified — routes auto-provision on this server.</p>
      ) : (
        <>
          {server.adapter_type === 'asterisk_freepbx' && <FreePBXChecklist />}
          {server.adapter_type === 'asterisk_manual' && <ManualAsteriskChecklist />}
          {boot.snippet && (
            <div className="space-y-1">
              <div className="flex items-center justify-between gap-2">
                <p className="text-xs text-p-text-light">Bridge dialplan — paste into <span className="font-mono">extensions_custom.conf</span>:</p>
                <CopyButton text={boot.snippet} />
              </div>
              <pre className="bg-gray-900 text-green-400 p-3 rounded-lg text-xs font-mono overflow-x-auto select-all whitespace-pre-wrap break-all">{boot.snippet}</pre>
            </div>
          )}
          {boot.ami_snippet && (
            <div className="space-y-1">
              <div className="flex items-center justify-between gap-2">
                <p className="text-xs text-p-text-light">AMI user — paste into <span className="font-mono">{boot.ami_snippet_file}</span> (credentials already configured):</p>
                <CopyButton text={boot.ami_snippet} />
              </div>
              <pre className="bg-gray-900 text-green-400 p-3 rounded-lg text-xs font-mono overflow-x-auto select-all whitespace-pre-wrap break-all">{boot.ami_snippet}</pre>
            </div>
          )}
          {boot.supports_sftp && (
            <div className="pt-1">
              <button onClick={() => setShowSftp(v => !v)} className="text-xs text-brand hover:underline">
                {showSftp ? '− Hide SSH install' : 'Apply via SSH/SFTP'}
              </button>
              {showSftp && (
                <div className="mt-2 p-2 border border-p-border-light rounded-lg space-y-2">
                  <p className="text-xs text-p-text-light">Credentials are used once for this install and never stored.</p>
                  <div className="grid grid-cols-2 gap-2">
                    <input placeholder="SSH host" value={sftp.host} onChange={e => setSftp({ ...sftp, host: e.target.value })} className="px-2 py-1 text-sm border border-p-border-light rounded-lg bg-p-bg text-p-text" />
                    <input placeholder="Port" value={sftp.port} onChange={e => setSftp({ ...sftp, port: e.target.value })} className="px-2 py-1 text-sm border border-p-border-light rounded-lg bg-p-bg text-p-text" />
                    <input placeholder="Username" value={sftp.username} onChange={e => setSftp({ ...sftp, username: e.target.value })} className="px-2 py-1 text-sm border border-p-border-light rounded-lg bg-p-bg text-p-text" />
                    <input type="password" placeholder="Password" value={sftp.password} onChange={e => setSftp({ ...sftp, password: e.target.value })} className="px-2 py-1 text-sm border border-p-border-light rounded-lg bg-p-bg text-p-text" />
                  </div>
                  <button
                    onClick={() => apply.mutate({ id: server.id, creds: sftp }, { onError: (e) => alert((e as Error).message), onSuccess: () => setShowSftp(false) })}
                    disabled={apply.isPending || !sftp.host || !sftp.username}
                    className="px-3 py-1 text-xs font-medium rounded-lg bg-brand text-white hover:bg-brand-hover disabled:opacity-40"
                  >
                    {apply.isPending ? 'Applying…' : 'Apply'}
                  </button>
                </div>
              )}
            </div>
          )}
        </>
      )}

      {boot.log && (
        <div>
          <button onClick={() => setShowLog(v => !v)} className="text-xs text-p-text-secondary hover:text-p-text">
            {showLog ? '− Hide log' : 'Show bootstrap log'}
          </button>
          {showLog && <pre className="mt-1 bg-gray-50 dark:bg-gray-800/50 p-2 rounded-lg text-xs text-p-text-light overflow-x-auto whitespace-pre-wrap">{boot.log}</pre>}
        </div>
      )}
    </div>
  )
}

export function PhoneServerPill({ server }: { server: PhoneServer }) {
  const update = useUpdatePhoneServer()
  const del = useDeletePhoneServer()
  const setDefault = useSetDefaultPhoneServer()
  const setSecret = useSetPhoneServerAmiSecret()
  const delSecret = useDeletePhoneServerAmiSecret()

  // Collapsed by default — only a server the user explicitly expanded
  // (persisted 'open') starts open.
  const [open, setOpen] = useState(() => localStorage.getItem(`phone-server-${server.id}`) === 'open')
  const [secretValue, setSecretValue] = useState('')
  const [saved, setSaved] = useState('')

  const toggleOpen = () => {
    const next = !open
    setOpen(next)
    localStorage.setItem(`phone-server-${server.id}`, next ? 'open' : 'closed')
  }

  const flash = (field: string) => { setSaved(field); setTimeout(() => setSaved(''), 2000) }

  const saveField = (data: Parameters<typeof update.mutate>[0]['data'], field: string) =>
    update.mutate({ id: server.id, data }, { onSuccess: () => flash(field) })

  const saveConfig = (key: string, value: string) =>
    saveField({ config: { ...server.config, [key]: value } }, `cfg_${key}`)

  const saveSecret = () => {
    if (!secretValue.trim()) return
    setSecret.mutate({ id: server.id, value: secretValue.trim() }, {
      onSuccess: () => { setSecretValue(''); flash('secret') },
    })
  }

  const isAsterisk = server.adapter_type.startsWith('asterisk')

  return (
    <div className="border border-p-border-light rounded-lg">
      {/* flex-wrap: on narrow screens the badge group drops to its own row
          instead of overlapping the truncated name */}
      <button onClick={toggleOpen} className="w-full flex flex-wrap items-center justify-between gap-x-2 gap-y-1.5 p-3 text-left">
        <div className="flex items-center gap-2 min-w-0">
          <span className="text-sm font-medium text-p-text truncate">{server.name}</span>
          <span className="text-xs text-p-text-light font-mono shrink-0">{server.adapter_type}</span>
        </div>
        <div className="flex flex-wrap items-center gap-1.5">
          {server.is_default && <Badge variant="blue">Default</Badge>}
          <Badge variant={BOOTSTRAP_VARIANT[server.bootstrap_status]}>{server.bootstrap_status}</Badge>
          {server.last_health_status && server.last_health_status !== 'unknown' && (
            <span title={server.last_health_detail || ''}>
              <Badge variant={HEALTH_VARIANT[server.last_health_status] || 'default'}>{server.last_health_status}</Badge>
            </span>
          )}
          <span className="text-p-text-secondary text-xs ml-1">{open ? '−' : '+'}</span>
        </div>
      </button>

      {open && (
        <div className="px-3 pb-3 space-y-3 border-t border-p-border-light pt-3">
          <div className="grid grid-cols-2 gap-3">
            <div>
              <label className="block text-xs font-medium text-p-text mb-1">Name</label>
              <input
                defaultValue={server.name}
                onBlur={e => { if (e.target.value && e.target.value !== server.name) saveField({ name: e.target.value }, 'name') }}
                className="w-full px-2.5 py-1.5 text-sm border border-p-border-light rounded-lg bg-p-bg text-p-text"
              />
              <SavedBadge show={saved === 'name'} />
            </div>
            <div>
              <label className="block text-xs font-medium text-p-text mb-1">Host</label>
              <input
                defaultValue={server.host}
                onBlur={e => { if (e.target.value !== server.host) saveField({ host: e.target.value }, 'host') }}
                className="w-full px-2.5 py-1.5 text-sm border border-p-border-light rounded-lg bg-p-bg text-p-text"
                placeholder="pbx.example.com"
              />
              <SavedBadge show={saved === 'host'} />
            </div>
          </div>

          {isAsterisk && (
            <div className="space-y-2">
              <p className="text-xs font-semibold text-p-text-secondary uppercase tracking-wider">Asterisk AMI</p>
              <div className="grid grid-cols-3 gap-2">
                <div>
                  <label className="block text-xs text-p-text-light mb-1">AMI Host</label>
                  <input
                    defaultValue={cfg(server, 'ami_host')}
                    onBlur={e => { if (e.target.value !== cfg(server, 'ami_host')) saveConfig('ami_host', e.target.value) }}
                    className="w-full px-2 py-1 text-sm border border-p-border-light rounded-lg bg-p-bg text-p-text"
                    placeholder="(defaults to host)"
                  />
                </div>
                <div>
                  <label className="block text-xs text-p-text-light mb-1">AMI Port</label>
                  <input
                    type="number"
                    defaultValue={cfg(server, 'ami_port') || '5038'}
                    onBlur={e => { if (e.target.value !== (cfg(server, 'ami_port') || '5038')) saveConfig('ami_port', e.target.value) }}
                    className="w-full px-2 py-1 text-sm border border-p-border-light rounded-lg bg-p-bg text-p-text"
                  />
                </div>
                <div>
                  <label className="block text-xs text-p-text-light mb-1">AMI User</label>
                  <input
                    placeholder="auto-generated on bootstrap"
                    defaultValue={cfg(server, 'ami_username')}
                    onBlur={e => { if (e.target.value !== cfg(server, 'ami_username')) saveConfig('ami_username', e.target.value) }}
                    className="w-full px-2 py-1 text-sm border border-p-border-light rounded-lg bg-p-bg text-p-text"
                  />
                </div>
              </div>
              <div className="flex items-center gap-2">
                <label className="text-xs text-p-text-light w-20 shrink-0">AMI Secret</label>
                <input
                  type="password"
                  value={secretValue}
                  onChange={e => setSecretValue(e.target.value)}
                  placeholder={server.ami_secret_configured ? '******** (auto-generated on bootstrap)' : 'auto-generated on bootstrap — set to override'}
                  className="flex-1 px-2 py-1 text-sm border border-p-border-light rounded-lg bg-p-bg text-p-text font-mono"
                />
                <button
                  onClick={saveSecret}
                  disabled={!secretValue.trim() || setSecret.isPending}
                  className="px-3 py-1 text-xs font-medium rounded-lg bg-brand text-white hover:bg-brand-hover disabled:opacity-40"
                >
                  Save
                </button>
                {server.ami_secret_configured && (
                  <button
                    onClick={() => { if (confirm('Remove AMI secret?')) delSecret.mutate(server.id) }}
                    className="px-3 py-1 text-xs rounded-lg border border-red-200 text-red-500 hover:bg-red-50 dark:border-red-800 dark:hover:bg-red-900/20"
                  >
                    Remove
                  </button>
                )}
                <SavedBadge show={saved === 'secret'} />
              </div>
            </div>
          )}

          <BootstrapSection server={server} />

          <div className="pt-2 border-t border-p-border-light flex items-center justify-between">
            {!server.is_default ? (
              <button
                onClick={() => setDefault.mutate(server.id)}
                className="text-xs text-brand hover:underline"
              >
                Set as default
              </button>
            ) : <span />}
            <button
              onClick={() => {
                if (!confirm(`Delete server "${server.name}"?`)) return
                del.mutate(server.id, { onError: (e) => alert((e as Error).message) })
              }}
              className="text-xs text-red-500 hover:underline"
            >
              Delete server
            </button>
          </div>
        </div>
      )}
    </div>
  )
}
