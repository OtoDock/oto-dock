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
  useSetPhoneServerTwilioAuthToken,
  useDeletePhoneServerTwilioAuthToken,
  useServerBootstrap,
  useVerifyServerBootstrap,
  useApplyServerBootstrap,
  useCheckServerHealth,
} from '../../api/phone'
import { Badge, SaveState } from '../ui/SettingsControls'
import { useSaveState } from '../../hooks/useSaveState'
import { CopyButton } from '../CopyButton'
import { useDialogs } from '../ui/dialogs'
import { toastError } from '../../lib/http/apiJson'
import Button from '../ui/Button'
import { Input } from '../ui/Field'
import { CARD_BORDERED } from '../ui/classes'

const BOOTSTRAP_VARIANT: Record<PhoneServer['bootstrap_status'], 'neutral' | 'success' | 'warning' | 'danger'> = {
  pending: 'neutral',
  snippet_provided: 'warning',
  verified: 'success',
  failed: 'danger',
  drift: 'warning',
}

const HEALTH_VARIANT: Record<string, 'neutral' | 'success' | 'danger'> = {
  healthy: 'success',
  unhealthy: 'danger',
  unknown: 'neutral',
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
    <div className="p-2.5 rounded-card bg-surface-muted space-y-1.5">
      <p className="text-xs font-semibold text-secondary">One-time FreePBX setup</p>
      <ol className="list-decimal list-inside text-xs text-tertiary space-y-1">
        <li>Paste the <span className="font-medium">bridge dialplan</span> below into <span className="font-mono">extensions_custom.conf</span>, in Admin, Config Edit.</li>
        <li>Paste the <span className="font-medium">AMI user</span> below into <span className="font-mono">manager_custom.conf</span>, then <span className="font-medium">Apply Config</span>. Its credentials are already set on this server. You don't need to type them anywhere.</li>
        <li>Route each inbound AI number to the bridge, once per number. Create a <span className="font-medium">Custom Destination</span> targeting <span className="font-mono">oto-audiosocket-bridge,&lt;number&gt;,1</span>. Then send the number to it. For an external number or DID, use its <span className="font-medium">Inbound Route</span>. For an internal extension, use a <span className="font-medium">Misc Application</span>. Then click Apply Config. Outbound-only numbers need no routing.</li>
        <li>Allow traffic from the PBX to OtoDock on <span className="font-mono">TCP 9092-9093</span>. Allow traffic from OtoDock to the PBX on <span className="font-mono">TCP 5038</span>. Open them in your network firewall. If FreePBX's own firewall is on, open them there too, in Connectivity, Firewall.</li>
      </ol>
      <p className="text-xs italic text-tertiary">Verify checks the AMI connection only. A test call confirms the dialplan and routing.</p>
    </div>
  )
}

/** Plain-Asterisk variant: same generated snippets, no FreePBX GUI — short
 * pointers only; per-route AstDB commands are shown when a route is added. */
function ManualAsteriskChecklist() {
  return (
    <div className="p-2.5 rounded-card bg-surface-muted space-y-1.5">
      <p className="text-xs font-semibold text-secondary">One-time Asterisk setup</p>
      <ol className="list-decimal list-inside text-xs text-tertiary space-y-1">
        <li>Paste the <span className="font-medium">bridge dialplan</span> below into your dialplan, like <span className="font-mono">extensions_custom.conf</span>, then <span className="font-mono">dialplan reload</span>.</li>
        <li>Paste the <span className="font-medium">AMI user</span> below into <span className="font-mono">manager.conf</span>, then <span className="font-mono">manager reload</span>. Its credentials are already configured on this server.</li>
        <li>Point each inbound number at <span className="font-mono">oto-audiosocket-bridge,&lt;number&gt;,1</span>. Each route's <span className="font-mono">database put</span> command shows when you add the route.</li>
        <li>Allow traffic from the PBX to OtoDock on <span className="font-mono">TCP 9092-9093</span>. Allow traffic from OtoDock to the PBX on <span className="font-mono">TCP 5038</span>.</li>
      </ol>
      <p className="text-xs italic text-tertiary">Verify checks the AMI connection only. A test call confirms the dialplan and routing.</p>
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

  if (isLoading || !boot) return <p className="text-xs text-tertiary">Loading bootstrap…</p>

  const verified = server.bootstrap_status === 'verified'

  return (
    <div className="space-y-2">
      <div className="flex items-center justify-between">
        <p className="text-xs font-semibold text-secondary uppercase tracking-wider">Bootstrap</p>
        <div className="flex items-center gap-2">
          <button onClick={() => { health.mutateAsync(server.id).catch(toastError('The server health was not checked')) }} className="text-xs text-secondary hover:text-primary">Re-check health</button>
          {!verified && (
            <Button
              variant="primary"
              size="xs"
              onClick={() => verify.mutate(server.id, { onError: toastError('The server was not verified') })}
              disabled={verify.isPending}
            >
              {verify.isPending ? 'Verifying…' : 'Verify'}
            </Button>
          )}
        </div>
      </div>

      {verified ? (
        <p className="text-xs text-status-success-fg">✓ Verified. Routes on this server are set up automatically.</p>
      ) : !boot.requires_bootstrap ? (
        // No-bootstrap adapters (Twilio) still need the Verify click: it
        // probes the credentials + public URL and flips the server to
        // `verified`, which is what unlocks route provisioning.
        <p className="text-xs text-tertiary">
          No manual setup needed. Click <span className="font-medium">Verify</span> to
          check the credentials and turn on routes for this server.
        </p>
      ) : (
        <>
          {server.adapter_type === 'asterisk_freepbx' && <FreePBXChecklist />}
          {server.adapter_type === 'asterisk_manual' && <ManualAsteriskChecklist />}
          {boot.snippet && (
            <div className="space-y-1">
              <div className="flex items-center justify-between gap-2">
                <p className="text-xs text-tertiary">Bridge dialplan. Paste it into <span className="font-mono">extensions_custom.conf</span>:</p>
                <CopyButton text={boot.snippet} />
              </div>
              <pre className="bg-code-bg text-code-fg p-3 rounded-card text-xs font-mono overflow-x-auto select-all whitespace-pre-wrap break-all">{boot.snippet}</pre>
            </div>
          )}
          {boot.ami_snippet && (
            <div className="space-y-1">
              <div className="flex items-center justify-between gap-2">
                <p className="text-xs text-tertiary">AMI user, with its credentials already set. Paste it into <span className="font-mono">{boot.ami_snippet_file}</span>:</p>
                <CopyButton text={boot.ami_snippet} />
              </div>
              <pre className="bg-code-bg text-code-fg p-3 rounded-card text-xs font-mono overflow-x-auto select-all whitespace-pre-wrap break-all">{boot.ami_snippet}</pre>
            </div>
          )}
          {boot.supports_sftp && (
            <div className="pt-1">
              <button onClick={() => setShowSftp(v => !v)} className="text-xs text-link hover:underline">
                {showSftp ? '− Hide SSH install' : 'Apply via SSH or SFTP'}
              </button>
              {showSftp && (
                <div className="mt-2 p-2 border border-subtle rounded-card space-y-2">
                  <p className="text-xs text-tertiary">Credentials are used once for this install and never stored.</p>
                  <div className="grid grid-cols-2 gap-2">
                    <Input placeholder="SSH host" value={sftp.host} onChange={e => setSftp({ ...sftp, host: e.target.value })} />
                    <Input placeholder="Port" value={sftp.port} onChange={e => setSftp({ ...sftp, port: e.target.value })} />
                    <Input placeholder="Username" value={sftp.username} onChange={e => setSftp({ ...sftp, username: e.target.value })} />
                    <Input type="password" placeholder="Password" value={sftp.password} onChange={e => setSftp({ ...sftp, password: e.target.value })} />
                  </div>
                  <Button
                    variant="primary"
                    size="xs"
                    onClick={() => apply.mutate({ id: server.id, creds: sftp }, { onError: toastError('The setup was not applied'), onSuccess: () => setShowSftp(false) })}
                    disabled={apply.isPending || !sftp.host || !sftp.username}
                  >
                    {apply.isPending ? 'Applying…' : 'Apply'}
                  </Button>
                </div>
              )}
            </div>
          )}
        </>
      )}

      {boot.log && (
        <div>
          <button onClick={() => setShowLog(v => !v)} className="text-xs text-secondary hover:text-primary">
            {showLog ? '− Hide log' : 'Show bootstrap log'}
          </button>
          {showLog && <pre className="mt-1 bg-surface-sunken p-2 rounded-card text-xs text-tertiary overflow-x-auto whitespace-pre-wrap">{boot.log}</pre>}
        </div>
      )}
    </div>
  )
}

export function PhoneServerPill({ server }: { server: PhoneServer }) {
  const dialogs = useDialogs()
  const update = useUpdatePhoneServer()
  const del = useDeletePhoneServer()
  const setDefault = useSetDefaultPhoneServer()
  const setSecret = useSetPhoneServerAmiSecret()
  const delSecret = useDeletePhoneServerAmiSecret()
  const setTwilioToken = useSetPhoneServerTwilioAuthToken()
  const delTwilioToken = useDeletePhoneServerTwilioAuthToken()

  // Collapsed by default — only a server the user explicitly expanded
  // (persisted 'open') starts open.
  const [open, setOpen] = useState(() => localStorage.getItem(`phone-server-${server.id}`) === 'open')
  const [secretValue, setSecretValue] = useState('')
  const [twilioTokenValue, setTwilioTokenValue] = useState('')
  const { stateOf, run } = useSaveState()

  const toggleOpen = () => {
    const next = !open
    setOpen(next)
    localStorage.setItem(`phone-server-${server.id}`, next ? 'open' : 'closed')
  }

  const saveField = (data: Parameters<typeof update.mutate>[0]['data'], field: string) => {
    void run(field, () => update.mutateAsync({ id: server.id, data }), { failTitle: 'The server setting was not saved' })
  }

  const saveConfig = (key: string, value: string) =>
    saveField({ config: { ...server.config, [key]: value } }, `cfg_${key}`)

  const saveSecret = () => {
    if (!secretValue.trim()) return
    void run('secret', () => setSecret.mutateAsync({ id: server.id, value: secretValue.trim() }).then(() => setSecretValue('')),
      { failTitle: 'The AMI secret was not saved' })
  }

  const saveTwilioToken = () => {
    if (!twilioTokenValue.trim()) return
    void run('twilio_token', () => setTwilioToken.mutateAsync({ id: server.id, value: twilioTokenValue.trim() }).then(() => setTwilioTokenValue('')),
      { failTitle: 'The Twilio auth token was not saved' })
  }

  const isAsterisk = server.adapter_type.startsWith('asterisk')
  const isTwilio = server.adapter_type === 'twilio'

  return (
    <div className={CARD_BORDERED}>
      {/* flex-wrap: on narrow screens the badge group drops to its own row
          instead of overlapping the truncated name */}
      <button type="button" onClick={toggleOpen} aria-expanded={open} className="w-full flex flex-wrap items-center justify-between gap-x-2 gap-y-1.5 p-3 text-left">
        <div className="flex items-center gap-2 min-w-0">
          <span className="text-sm font-medium text-primary truncate">{server.name}</span>
          <span className="text-xs text-tertiary font-mono shrink-0">{server.adapter_type}</span>
        </div>
        <div className="flex flex-wrap items-center gap-1.5">
          {server.is_default && <Badge variant="action">Default</Badge>}
          <Badge variant={BOOTSTRAP_VARIANT[server.bootstrap_status]}>{server.bootstrap_status}</Badge>
          {server.last_health_status && server.last_health_status !== 'unknown' && (
            <span title={server.last_health_detail || ''}>
              <Badge variant={HEALTH_VARIANT[server.last_health_status] || 'neutral'}>{server.last_health_status}</Badge>
            </span>
          )}
          <span className="text-secondary text-xs ml-1">{open ? '−' : '+'}</span>
        </div>
      </button>

      {open && (
        <div className="px-3 pb-3 space-y-3 border-t border-subtle pt-3">
          <div className="grid grid-cols-2 gap-3">
            <div>
              <p className="text-xs font-medium text-primary mb-1"><label>Name</label><SaveState state={stateOf('name')} /></p>
              <Input
                defaultValue={server.name}
                onBlur={e => { if (e.target.value && e.target.value !== server.name) saveField({ name: e.target.value }, 'name') }}
              />
            </div>
            {/* Twilio has no host — its coordinates live in the section below. */}
            {!isTwilio && (
              <div>
                <p className="text-xs font-medium text-primary mb-1"><label>Host</label><SaveState state={stateOf('host')} /></p>
                <Input
                  defaultValue={server.host}
                  onBlur={e => { if (e.target.value !== server.host) saveField({ host: e.target.value }, 'host') }}
                  placeholder="pbx.example.com"
                />
              </div>
            )}
          </div>

          {isAsterisk && (
            <div className="space-y-2">
              <p className="text-xs font-semibold text-secondary uppercase tracking-wider">Asterisk AMI</p>
              <div className="grid grid-cols-3 gap-2">
                <div>
                  <p className="text-xs text-tertiary mb-1"><label>AMI Host</label><SaveState state={stateOf('cfg_ami_host')} /></p>
                  <Input
                    defaultValue={cfg(server, 'ami_host')}
                    onBlur={e => { if (e.target.value !== cfg(server, 'ami_host')) saveConfig('ami_host', e.target.value) }}
                    placeholder="Defaults to the host"
                  />
                </div>
                <div>
                  <p className="text-xs text-tertiary mb-1"><label>AMI Port</label><SaveState state={stateOf('cfg_ami_port')} /></p>
                  <Input
                    type="number"
                    defaultValue={cfg(server, 'ami_port') || '5038'}
                    onBlur={e => { if (e.target.value !== (cfg(server, 'ami_port') || '5038')) saveConfig('ami_port', e.target.value) }}
                  />
                </div>
                <div>
                  <p className="text-xs text-tertiary mb-1"><label>AMI User</label><SaveState state={stateOf('cfg_ami_username')} /></p>
                  <Input
                    placeholder="Auto-generated on bootstrap"
                    defaultValue={cfg(server, 'ami_username')}
                    onBlur={e => { if (e.target.value !== cfg(server, 'ami_username')) saveConfig('ami_username', e.target.value) }}
                  />
                </div>
              </div>
              <div className="flex items-center gap-2">
                <label className="text-xs text-tertiary w-20 shrink-0">AMI Secret</label>
                <Input
                  type="password"
                  value={secretValue}
                  onChange={e => setSecretValue(e.target.value)}
                  placeholder={server.ami_secret_configured ? '********, auto-generated on bootstrap' : 'Auto-generated on bootstrap, or type your own'}
                  className="flex-1 font-mono"
                />
                <Button
                  variant="primary"
                  size="xs"
                  onClick={saveSecret}
                  disabled={!secretValue.trim() || setSecret.isPending}
                >
                  Save
                </Button>
                {server.ami_secret_configured && (
                  <button
                    onClick={async () => {
                      if (await dialogs.confirm({ title: 'Remove the AMI secret?', confirmLabel: 'Remove', tone: 'danger' })) delSecret.mutateAsync(server.id).catch(toastError('The AMI secret was not removed'))
                    }}
                    className="px-3 py-1 text-xs rounded-control border border-status-danger-border text-status-danger-fg hover:bg-status-danger-bg"
                  >
                    Remove
                  </button>
                )}
                <SaveState state={stateOf('secret')} />
              </div>
            </div>
          )}

          {isTwilio && (
            <div className="space-y-2">
              <p className="text-xs font-semibold text-secondary uppercase tracking-wider">Twilio account</p>
              <div className="grid grid-cols-2 gap-2">
                <div>
                  <p className="text-xs text-tertiary mb-1"><label>Account SID</label><SaveState state={stateOf('cfg_account_sid')} /></p>
                  <Input
                    defaultValue={cfg(server, 'account_sid')}
                    onBlur={e => { if (e.target.value !== cfg(server, 'account_sid')) saveConfig('account_sid', e.target.value.trim()) }}
                    className="font-mono"
                    placeholder="ACxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx"
                  />
                </div>
              </div>
              <div className="flex items-center gap-2">
                <label className="text-xs text-tertiary w-20 shrink-0">Auth token</label>
                <Input
                  type="password"
                  value={twilioTokenValue}
                  onChange={e => setTwilioTokenValue(e.target.value)}
                  placeholder={server.twilio_auth_token_configured ? '********' : 'From the Twilio console'}
                  className="flex-1 font-mono"
                />
                <Button
                  variant="primary"
                  size="xs"
                  onClick={saveTwilioToken}
                  disabled={!twilioTokenValue.trim() || setTwilioToken.isPending}
                >
                  Save
                </Button>
                {server.twilio_auth_token_configured && (
                  <button
                    onClick={async () => {
                      if (await dialogs.confirm({ title: 'Remove the Twilio auth token?', confirmLabel: 'Remove', tone: 'danger' })) delTwilioToken.mutateAsync(server.id).catch(toastError('The Twilio auth token was not removed'))
                    }}
                    className="px-3 py-1 text-xs rounded-control border border-status-danger-border text-status-danger-fg hover:bg-status-danger-bg"
                  >
                    Remove
                  </button>
                )}
                <SaveState state={stateOf('twilio_token')} />
              </div>
              <p className="text-xs italic text-tertiary">
                Calls come in through your public dashboard URL, which must use https. Is a sign-in
                gateway in front, such as Authentik or Authelia? Then exempt <span className="font-mono">/v1/twilio/</span>,
                like the other documented paths. Click Verify below, then add routes. Each inbound
                route's number is connected to this install automatically.
              </p>
            </div>
          )}

          <BootstrapSection server={server} />

          <div className="pt-2 border-t border-subtle flex items-center justify-between">
            {!server.is_default ? (
              <button
                onClick={() => { setDefault.mutateAsync(server.id).catch(toastError('The default server was not set')) }}
                className="text-xs text-link hover:underline"
              >
                Set as default
              </button>
            ) : <span />}
            <button
              onClick={async () => {
                if (!(await dialogs.confirm({ title: `Delete the server "${server.name}"?`, confirmLabel: 'Delete', tone: 'danger' }))) return
                del.mutate(server.id, { onError: toastError('The server was not deleted') })
              }}
              className="text-xs text-status-danger-fg hover:underline"
            >
              Delete server
            </button>
          </div>
        </div>
      )}
    </div>
  )
}
