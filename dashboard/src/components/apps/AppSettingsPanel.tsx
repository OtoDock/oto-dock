import { useState } from 'react'
import { useAppSecrets, useDeleteAppSecret, useSetAppSecret, type AppInboundHook, type AppSecret, type PinnedApp } from '../../api/apps'
import { useTriggers } from '../../api/triggers'
import { useDialogs } from '../ui/dialogs'
import Dialog from '../ui/Dialog'
import Button from '../ui/Button'
import { DIALOG_PANEL } from '../ui/classes'
import { Input } from '../ui/Field'

/**
 * The app's settings (APPS.md "Secrets"): a dialog over the frame, opened
 * from the menu's Settings and from the cards' Set buttons, for whoever may
 * manage the row — the owner of a personal app, an editor or a manager of a
 * shared one. It lists every secret the manifest declares with where its
 * value goes and whether a person has set one, takes a value into a
 * password field that is never echoed back (a set value shows as "(set)";
 * typing a new one replaces it), and removes one. No value ever returns
 * from the proxy; the running server restarts with a changed value on the
 * next request. A stored name the manifest no longer declares is listed as
 * such, with Remove alone. The platform's own words are one line per
 * section; the app's description of a secret is clamped to two lines
 * (found on the VM pass 2026-09-18: an agent wrote a paragraph per secret
 * and the panel on a phone was all text).
 */

interface Props {
  app: PinnedApp
  agent: string
  onClose: () => void
}

/** Where the value goes, in a few words; the card's Details carry the
 * whole sentence. */
function secretUseText(s: AppSecret): string {
  if (s.sends_to) return `goes to ${s.sends_to.host} only`
  if (s.env) return 'read by the server itself'
  return 'used by the platform only'
}

/** An app's own description of a secret runs long when the agent wrote a
 * paragraph: clamped to two lines, the rest behind "more". */
export const CLAMP_CHARS = 120

function SecretDescription({ text }: { text: string }) {
  const [open, setOpen] = useState(false)
  const long = text.length > CLAMP_CHARS
  return (
    <p className="mt-0.5 text-secondary" data-testid="secret-description">
      <span className={long && !open ? 'line-clamp-2' : ''}>{text}</span>
      {long && (
        <button
          type="button"
          onClick={() => setOpen((v) => !v)}
          className="text-tertiary underline decoration-dotted underline-offset-2 hover:text-secondary"
          data-testid="secret-description-more"
        >
          {open ? 'less' : 'more'}
        </button>
      )}
    </p>
  )
}

function when(iso: string | undefined): string {
  if (!iso) return ''
  const d = new Date(iso)
  return Number.isNaN(d.getTime()) ? '' : d.toLocaleString()
}

/** What a hook's vendor must send, in words (APPS.md "Inbound hooks"). */
function inboundSchemeText(h: AppInboundHook): string {
  if (h.verify === 'stripe') return 'Stripe-signed events with the Stripe-Signature header'
  if (h.verify === 'github') return 'GitHub-signed events with the X-Hub-Signature-256 header'
  if (h.verify === 'bearer') return 'Events with the secret as a bearer token in the Authorization header'
  return `Events signed with HMAC-SHA256 in the ${h.header} header${h.prefix ? ` after "${h.prefix}"` : ''}`
}

/** The URL a vendor posts to: the origin the person is using plus the
 * route (APPS.md "Inbound hooks"). */
function inboundUrl(appId: string, name: string): string {
  const origin = typeof window !== 'undefined' ? window.location.origin : ''
  return `${origin}/v1/apps/${appId}/inbound/${name}`
}

export default function AppSettingsPanel({ app, agent, onClose }: Props) {
  const dialogs = useDialogs()
  const { data, isLoading, error } = useAppSecrets(app.id)
  const set = useSetAppSecret(agent)
  const del = useDeleteAppSecret(agent)
  const [values, setValues] = useState<Record<string, string>>({})
  const [notice, setNotice] = useState('')
  const [err, setErr] = useState('')
  const [copied, setCopied] = useState('')
  const title = app.title || app.slug
  const hooks = Object.entries(app.inbound ?? {})
  // The triggers aimed at this app (APPS.md "Handlers"; a template's
  // blueprint seeds one per copy): the address each is fired at and the
  // key it takes, so the member never hunts the Triggers tab for them.
  const triggersQ = useTriggers({ agent })
  const appTriggers = (triggersQ.data ?? []).filter((t) => t.app_id === app.id)
  const origin = typeof window !== 'undefined' ? window.location.origin : ''

  const copy = async (name: string, text: string) => {
    try {
      await navigator.clipboard.writeText(text)
      setCopied(name)
      setTimeout(() => setCopied(''), 1500)
    } catch {
      setErr('Could not copy. Select the address and copy it by hand.')
    }
  }

  const save = async (name: string) => {
    const value = values[name] ?? ''
    if (!value.trim()) return
    setErr('')
    setNotice('')
    try {
      await set.mutateAsync({ appId: app.id, name, value })
      setValues((prev) => ({ ...prev, [name]: '' }))
      setNotice(`${name} is set. The app’s server picks it up on its next start.`)
    } catch (e) {
      setErr((e as Error).message)
    }
  }
  const remove = async (name: string) => {
    if (!(await dialogs.confirm({ title: `Remove the value of ${name}?`, body: 'The app loses it at once.', confirmLabel: 'Remove', tone: 'danger' }))) return
    setErr('')
    setNotice('')
    try {
      await del.mutateAsync({ appId: app.id, name })
      setNotice(`${name} removed.`)
    } catch (e) {
      setErr((e as Error).message)
    }
  }

  const secrets = data?.secrets ?? []
  const busy = set.isPending || del.isPending

  return (
    <Dialog
      onClose={onClose}
      placement="sheet"
      aria-label={`Settings of ${title}`}
      data-testid="app-settings-panel"
      panelClassName={`${DIALOG_PANEL} flex max-h-[90vh] max-w-lg flex-col text-xs`}
    >
        <div className="flex items-center gap-2 border-b border-subtle px-4 py-2.5">
          <span className="font-medium text-primary">Settings of “{title}”</span>
          <Button className="ml-auto" onClick={onClose} aria-label="Close settings">
            ✕
          </Button>
        </div>
        <div className="min-h-0 flex-1 overflow-y-auto px-4 py-3">
          <section>
            <h3 className="text-3xs font-semibold uppercase tracking-wide text-tertiary">Secrets</h3>
            <p className="mt-0.5 text-tertiary">
              A value is typed here by a person and never shown again.
            </p>
            {isLoading && <p className="mt-2 text-tertiary">Loading…</p>}
            {error && <p className="mt-2 text-status-danger-fg">{(error as Error).message}</p>}
            {!isLoading && !error && secrets.length === 0 && (
              <p className="mt-2 text-tertiary">This app declares no secrets.</p>
            )}
            <ul className="mt-2 space-y-3">
              {secrets.map((s) => (
                <li key={s.name} className="rounded-card border border-subtle p-2.5" data-testid={`secret-row-${s.name}`}>
                  <div className="flex flex-wrap items-baseline gap-x-2 gap-y-0.5">
                    <code className="font-mono text-2xs font-semibold text-primary">{s.name}</code>
                    {s.declared === false ? (
                      <span className="text-tertiary">The app no longer declares it</span>
                    ) : (
                      <>
                        {s.required && <span className="rounded-sm bg-status-warning-bg px-1 py-px text-3xs text-status-warning-fg">required</span>}
                        <span className={s.env ? 'text-status-warning-fg' : 'text-secondary'}>{secretUseText(s)}</span>
                      </>
                    )}
                  </div>
                  {s.description && <SecretDescription text={s.description} />}
                  <p className="mt-0.5 text-tertiary" data-testid={`secret-state-${s.name}`}>
                    {s.set
                      ? `Set${s.set_by ? ` by ${s.set_by}` : ''}${s.updated_at ? ` on ${when(s.updated_at)}` : ''}`
                      : s.required ? 'Not set, required' : 'Not set'}
                  </p>
                  <div className="mt-1.5 flex flex-wrap items-center gap-1.5">
                    {s.declared !== false && (
                      <>
                        <Input
                          compact
                          type="password"
                          autoComplete="off"
                          spellCheck={false}
                          aria-label={`Value of ${s.name}`}
                          placeholder={s.set ? 'Already set. Type a new value to replace it.' : 'value'}
                          value={values[s.name] ?? ''}
                          onChange={(e) => setValues((prev) => ({ ...prev, [s.name]: e.target.value }))}
                          onKeyDown={(e) => { if (e.key === 'Enter') { e.preventDefault(); void save(s.name) } }}
                          className="flex-1"
                        />
                        <Button
                          variant="primary"
                          type="button"
                          onClick={() => void save(s.name)}
                          disabled={busy || !(values[s.name] ?? '').trim()}
                        >
                          {s.set ? 'Replace' : 'Save'}
                        </Button>
                      </>
                    )}
                    {s.set && (
                      <Button type="button" onClick={() => void remove(s.name)} disabled={busy}>
                        Remove
                      </Button>
                    )}
                  </div>
                </li>
              ))}
            </ul>
          </section>
          {appTriggers.length > 0 && (
            <section className="mt-4" data-testid="triggers-section">
              <h3 className="text-3xs font-semibold uppercase tracking-wide text-tertiary">Triggers</h3>
              <p className="mt-0.5 text-tertiary">
                {app.scope === 'personal'
                  ? 'POST to the address with your API key to wake the app. Your keys are in User Settings, Integrations.'
                  : 'POST to the address with an agent API key to wake the app. Agent keys are on the agent’s Triggers page.'}
              </p>
              <ul className="mt-2 space-y-3">
                {appTriggers.map((t) => {
                  const url = t.webhook_path ? `${origin}${t.webhook_path}` : ''
                  return (
                    <li key={t.id} className="rounded-card border border-subtle p-2.5" data-testid={`trigger-row-${t.slug}`}>
                      <div className="flex flex-wrap items-baseline gap-x-2 gap-y-0.5">
                        <code className="font-mono text-2xs font-semibold text-primary">{t.name}</code>
                        <span className="text-secondary">wakes {t.handler}</span>
                        {!t.enabled && <span className="text-status-warning-fg">paused</span>}
                      </div>
                      {url ? (
                        <div className="mt-1 flex items-center gap-2">
                          <code className="min-w-0 flex-1 truncate rounded-sm bg-surface-sunken px-2 py-1 font-mono text-2xs text-secondary" title={url}>{url}</code>
                          <Button type="button" onClick={() => void copy(`trigger:${t.id}`, url)}>
                            {copied === `trigger:${t.id}` ? 'Copied' : 'Copy'}
                          </Button>
                        </div>
                      ) : (
                        <p className="mt-0.5 text-tertiary">Fed by a vendor subscription, with no address of its own</p>
                      )}
                      {t.last_error && <p className="mt-0.5 text-tertiary">{t.last_error}</p>}
                    </li>
                  )
                })}
              </ul>
            </section>
          )}
          {hooks.length > 0 && (
            <section className="mt-4" data-testid="inbound-section">
              <h3 className="text-3xs font-semibold uppercase tracking-wide text-tertiary">Webhooks</h3>
              <p className="mt-0.5 text-tertiary">
                Paste the address into the vendor’s webhook settings. Its signing secret goes above.
              </p>
              <ul className="mt-2 space-y-3">
                {hooks.map(([name, h]) => {
                  const url = inboundUrl(app.id, name)
                  const secret = secrets.find((s) => s.name === h.secret)
                  return (
                    <li key={name} className="rounded-card border border-subtle p-2.5" data-testid={`inbound-row-${name}`}>
                      <div className="flex flex-wrap items-baseline gap-x-2 gap-y-0.5">
                        <code className="font-mono text-2xs font-semibold text-primary">{name}</code>
                        <span className="text-secondary">{inboundSchemeText(h)}</span>
                      </div>
                      <div className="mt-1 flex items-center gap-2">
                        <code className="min-w-0 flex-1 truncate rounded-sm bg-surface-sunken px-2 py-1 font-mono text-2xs text-secondary" title={url}>{url}</code>
                        <Button type="button" onClick={() => void copy(name, url)}>
                          {copied === name ? 'Copied' : 'Copy'}
                        </Button>
                      </div>
                      <p className="mt-0.5 text-tertiary">
                        Verified with the secret <code className="font-mono text-2xs">{h.secret}</code> and
                        wakes <code className="font-mono text-2xs">{h.handler}</code>.
                        {secret ? (secret.set ? ' The secret is set.' : ' The secret is not set, so the address is off.') : ''}
                      </p>
                    </li>
                  )
                })}
              </ul>
            </section>
          )}
          {notice && <p className="mt-3 text-status-success-fg" data-testid="settings-notice">{notice}</p>}
          {err && <p className="mt-3 text-status-danger-fg" data-testid="settings-error">{err}</p>}
        </div>
    </Dialog>
  )
}
