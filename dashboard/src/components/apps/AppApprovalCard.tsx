import { useState, type ReactNode } from 'react'
import { useApproveApp, type AppAction, type AppInboundHook, type AppSecret, type PinnedApp } from '../../api/apps'
import AppSettingsPanel from './AppSettingsPanel'
import { type ActionFloor } from '../../lib/permissions'
import { placePhrase } from '../../api/remoteMachines'
import { SITE, TARGET_LOCAL } from '../../lib/placement'
import { appKind } from '../../lib/kinds/app'
import Button from '../ui/Button'
import { BUTTON_BASE, BUTTON_SIZE, BUTTON_VARIANT } from '../ui/classes'

/**
 * Approval card — shared between the standing apps overlay, the Dock
 * (chat/project-scoped pins) and the full-screen page, so every surface
 * renders the SAME plain-language summary over the same sig-checked
 * approve call. Driven by the manifest's BLOCKS (APPS.md "The approval
 * card"): the body is one plain list, one short line per kind of thing the
 * app may do (`ManifestSummary`); the full groups, one sentence per
 * declaration (`ManifestBlocks`), sit behind a "Details" fold and the raw
 * manifest behind "Exact manifest" — both toggles in the pinned footer next
 * to Approve. Render it only when the app needs approval
 * (`appNeedsApproval`).
 *
 * Layout: a shrinkable flex column — a pinned header (the headline), a
 * scrolling body and a pinned footer. The card takes at most the height of
 * the column it sits in (`min-h-0 shrink`); the app frame below gets the
 * rest. Found on the phone 2026-09-16: the details scrolled inside their
 * own box but Approve sat below the fold of a page that does not scroll;
 * found again 2026-09-18: the groups plus two folds stacked on a phone left
 * nothing readable, hence the summary list. `AppCardShell` is the piece the
 * deploy card reuses.
 */

/** The manifest as declared — the expander's text (non-empty blocks only). */
function exactManifest(app: PinnedApp): Record<string, unknown> {
  const out: Record<string, unknown> = { actions: app.actions }
  const files = app.files
  if (files && (files.read.length || files.write.length)) {
    // The declaration as the manifest has it: an empty list of folders to
    // make is left out.
    const { create, ...prefixes } = files
    out.files = create?.length ? files : prefixes
  }
  if (app.egress?.length) out.egress = app.egress
  if (app.handlers && Object.keys(app.handlers).length) out.handlers = app.handlers
  if (app.exports && Object.keys(app.exports).length) out.exports = app.exports
  if (app.bindings?.length) out.bindings = app.bindings
  if (app.requires && Object.keys(app.requires).length) out.requires = app.requires
  if (app.steps && Object.keys(app.steps).length) out.steps = app.steps
  if (app.inbound && Object.keys(app.inbound).length) out.inbound = app.inbound
  const ext = app.external
  if (ext && (ext.links.length || ext.challenge.length || ext.session_days !== 30)) {
    // The row fills every key with its default; the declaration is what
    // differs from the defaults.
    out.external = {
      ...(ext.links.length ? { links: ext.links } : {}),
      ...(ext.challenge.length ? { challenge: ext.challenge } : {}),
      ...(ext.session_days !== 30 ? { session_days: ext.session_days } : {}),
    }
  }
  const secrets = declaredSecrets(app)
  if (secrets.length) {
    // The declaration as the manifest has it — never the set flags, never
    // a value (the row carries none).
    out.secrets = secrets.map(({ name, required, description, sends_to, env }) => ({
      name, required, ...(description ? { description } : {}),
      ...(sends_to ? { sends_to } : {}), ...(env ? { env } : {}),
    }))
  }
  return out
}

/** The secrets the manifest declares (a stored name it no longer declares
 * is the settings panel's business, not the card's). */
function declaredSecrets(app: PinnedApp): AppSecret[] {
  return (app.secrets ?? []).filter((s) => s.declared !== false)
}

/** What an inbound hook receives, in words (APPS.md "Inbound hooks"). */
function inboundWords(h: AppInboundHook): ReactNode {
  if (h.verify === 'stripe') return 'Stripe-signed events'
  if (h.verify === 'github') return 'GitHub-signed events'
  if (h.verify === 'bearer') return 'bearer-authenticated events'
  return <>HMAC-signed events with the <Code>{h.header}</Code> header</>
}

/** Where a secret's value goes, in words (APPS.md "Secrets"). */
function secretUseWords(s: AppSecret): ReactNode {
  if (s.sends_to) {
    return <>The platform sends it to <Code>{s.sends_to.host}</Code> as <Code>{s.sends_to.header}</Code>. The server never sees it.</>
  }
  if (s.env) return <span className="text-status-warning-fg">The server reads it itself.</span>
  return 'Only the platform uses it.'
}

/** Compact `key: value` chip for approval-card parameter summaries. */
function ParamChip({ k, v }: { k: string; v?: string }) {
  return (
    <code className="inline-flex max-w-56 items-baseline gap-0.5 truncate rounded-sm bg-surface-muted px-1 py-px font-mono text-3xs">
      <span className="font-semibold">{k}</span>
      {v !== undefined && <span className="truncate">: {v}</span>}
    </code>
  )
}

function chipValue(v: unknown): string {
  const s = typeof v === 'string' ? v : JSON.stringify(v)
  return s.length > 24 ? `${s.slice(0, 24)}…` : s
}

const FLOOR_WHO: Record<ActionFloor, string> = {
  manager: 'managers',
  editor: 'editors and managers',
  contributor: 'contributors, editors and managers',
}
const FLOOR_LABEL: Record<ActionFloor, string> = {
  manager: 'managers only',
  editor: 'editors and up',
  contributor: 'contributors and up',
}

/** A button that acts for the agent with no floor: anyone the app reaches. */
const NO_FLOOR_LABEL = 'everyone who opens the app'
const NO_FLOOR_WHO = 'Anyone who opens the app can press it'
/** A folder the platform makes once the manifest is approved (`files.create`). */
export const CREATES_FOLDER = 'Creates the folder'
const CHIP = 'rounded-sm bg-surface-muted px-1 py-px text-3xs font-medium text-secondary'

/** Who may press: the floor, or for a button that acts for the agent with
 *  none, everyone who opens the app. */
function FloorChip({ role, acts = false }: { role?: ActionFloor; acts?: boolean }) {
  if (!role) {
    return acts ? <span title={NO_FLOOR_WHO} className={CHIP}>{NO_FLOOR_LABEL}</span> : null
  }
  return (
    <span title={`Only ${FLOOR_WHO[role]} of this agent can use it`} className={CHIP}>
      {FLOOR_LABEL[role]}
    </span>
  )
}

/** One plain-language approval line — raw manifests live behind the card's
 * "Exact manifest" expander, never inline. Shared with the deploy
 * card (a pending release carries its manifest). */
export function ActionLine({ ac }: { ac: AppAction }) {
  const pageFills = Object.keys(ac.args_schema?.properties ?? {})
  return (
    <li className="flex flex-wrap items-baseline gap-x-1.5 gap-y-0.5">
      <span className="font-medium text-primary">{ac.label}</span>
      {ac.type === 'mcp_tool' ? (
        <>
          <span>Calls <code className="font-mono text-2xs">{ac.tool}</code> on {ac.mcp}</span>
          {Object.entries(ac.fixed_args ?? {}).map(([k, v]) => (
            <ParamChip key={k} k={k} v={chipValue(v)} />
          ))}
          {pageFills.map((k) => <ParamChip key={k} k={k} v="filled by the page" />)}
          {ac.mcp_available === false && (
            <span className="text-status-warning-fg">Its MCP is unavailable right now</span>
          )}
        </>
      ) : ac.type === 'fire_task' ? (
        <>
          <span>Runs the task “{ac.task_name || ac.task_id}”.</span>
          {(ac.checks?.length ?? 0) > 0 && (
            <span>
              It also runs the check{ac.checks!.length > 1 ? 's' : ''} {ac.checks!.map((c, i) => (
                <span key={c}>{i > 0 ? ', ' : ''}<Code>{c}</Code></span>
              ))} on its result. A judge may run up to three times per run.
            </span>
          )}
          {pageFills.map((k) => <ParamChip key={k} k={k} v="filled by the page" />)}
        </>
      ) : ac.type === 'data_feed' ? (
        <span>
          Receives your own read-only view of the live <code className="font-mono text-2xs">{ac.feed}</code> platform feed
        </span>
      ) : ac.type === 'platform' ? (
        <>
          <span>
            Asks the platform for <code className="font-mono text-2xs">{ac.method}</code>
            : {PLATFORM_METHOD_WORDS[ac.method || ''] || 'your own view of it'}
          </span>
          {pageFills.map((k) => <ParamChip key={k} k={k} v="filled by the page" />)}
        </>
      ) : (
        <span className="break-all">
          Sends to the chat: “{(ac.prompt || '').slice(0, 100)}{(ac.prompt || '').length > 100 ? '…' : ''}”
        </span>
      )}
      <FloorChip role={ac.min_role} acts={BUTTON_TYPES.includes(ac.type)} />
    </li>
  )
}

// The catalog methods in words (APPS.md "Platform catalog").
const PLATFORM_METHOD_WORDS: Record<string, string> = {
  'viewer.me': 'who you are, with your name, username and role on this agent',
  'integrations.status': 'which connected-account providers you have connected',
  'tasks.run_result': 'the result of a task run you may see',
  'notifications.create': 'a notification to yourself. Acting alone, the app may notify this agent’s members or a personal app’s owner.',
  'files.list': 'the entries of a folder under its declared read prefixes',
  'files.read': 'a text file under its declared read prefixes',
  'files.write': 'to write a text file under its declared write prefixes',
  'setup.status': 'whether your setup of this agent is pending, and which tools are ready for you',
  'setup.complete': 'to mark your setup of this agent complete when you press its button. A manager may also complete the agent’s own setup.',
  'app.audience': 'who uses this app and with what role. That is its members and the people it is shared with. It also lists the agents it is placed in, and their members. Only the app’s editors and managers may read it on a page. So may an admin, or the owner of a personal app. The app’s own server may read it too.',
  'viewer.data.read': 'your own saved data in this app, a small document each viewer keeps for themselves',
  'viewer.data.write': 'to save your own data in this app, in a document of your own that nobody else reads',
}

// The same methods in two or three words, for the summary line.
const PLATFORM_METHOD_SHORT: Record<string, string> = {
  'viewer.me': 'who you are',
  'integrations.status': 'your connected accounts',
  'tasks.run_result': 'task results',
  'notifications.create': 'sending notifications',
  'files.list': 'listing its folders',
  'files.read': 'reading its files',
  'files.write': 'writing its files',
  'setup.status': 'your setup state and which tools are ready',
  'setup.complete': 'completing your setup',
  'app.audience': 'who uses it',
  'viewer.data.read': 'your saved data',
  'viewer.data.write': 'saving your data',
}

// The platform events a handler may wake on (APPS.md "Handlers").
const EVENT_WORDS: Record<string, string> = {
  chat_created: 'a chat starts',
  task_finished: 'a task finishes',
  file_changed: 'a workspace file changes',
  trigger_fired: 'a trigger fires',
  notification_created: 'you receive a notification',
  turn_finished: 'a chat turn ends',
  check_verdict: 'a check gives a verdict',
}

function eventWords(e: string): ReactNode {
  const m = /^app:([^:]+):(.+)$/.exec(e)
  if (m) return <><Code>{m[2]}</Code> comes from the app behind <Code>{m[1]}</Code></>
  const s = /^step:(.+)$/.exec(e)
  if (s) return <>the <Code>{s[1]}</Code> step finishes</>
  const c = /^check:(.+)$/.exec(e)
  if (c) return <>the <Code>{c[1]}</Code> check asks it to judge a turn</>
  return EVENT_WORDS[e] || e
}

/** A step's timeout in words (APPS.md "Steps": 60 s by default, two hours at most). */
function timeoutWords(seconds: number | undefined): string {
  const s = seconds ?? 60
  if (s % 3600 === 0) return `${s / 3600} hour${s === 3600 ? '' : 's'}`
  if (s % 60 === 0) return `${s / 60} minute${s === 60 ? '' : 's'}`
  return `${s} seconds`
}

/** A schedule in words where the cron is a plain interval; the cron
 * itself otherwise. */
function cronWords(cron: string): ReactNode {
  const every = /^\*\/(\d+) \* \* \* \*$/.exec(cron)
  if (every) return `every ${every[1]} minutes`
  if (/^\d+ \* \* \* \*$/.test(cron)) return 'every hour'
  if (/^\d+ \*\/(\d+) \* \* \*$/.test(cron)) return `every ${cron.split(' ')[1].slice(2)} hours`
  return <>on a <Code>{cron}</Code> schedule</>
}

/** Where the app's steps run, in words. */
function stepPlaceWords(app: PinnedApp): ReactNode {
  const t = app.step_target
  if (!t) return 'where this agent runs'
  if (t.kind === SITE.MACHINE) return <>on <Code>{t.name || 'a paired machine'}</Code></>
  return `on ${placePhrase(TARGET_LOCAL, '')}`
}

function Code({ children }: { children: ReactNode }) {
  return <code className="font-mono text-2xs">{children}</code>
}

const BUTTON_TYPES: ReadonlyArray<AppAction['type']> = ['mcp_tool', 'fire_task', 'send_prompt']

function Group({ heading, count, children }: { heading: string; count?: number; children: ReactNode }) {
  return (
    <div>
      <p className="text-3xs font-semibold uppercase tracking-wide text-tertiary">
        {heading}{count !== undefined ? ` (${count})` : ''}
      </p>
      <ul className="mt-0.5 space-y-0.5 text-secondary">{children}</ul>
    </div>
  )
}

/** The Secrets group (APPS.md "Secrets"): each declared name, where its
 * value goes, and whether a person has set one — with a Set button when
 * the host offers the settings panel. Shared by the approval card and the
 * deploy card. */
export function SecretsGroup({ app, onSetSecret }: { app: PinnedApp; onSetSecret?: (name: string) => void }) {
  const secrets = declaredSecrets(app)
  if (!secrets.length) return null
  return (
    <Group heading="Secrets" count={secrets.length}>
      {secrets.map((s) => (
        <li key={`sec-${s.name}`} className="flex flex-wrap items-baseline gap-x-1.5 gap-y-0.5" data-testid="secret-line">
          <span>
            Needs the secret <Code>{s.name}</Code>{s.description ? `, ${s.description}` : ''}. {secretUseWords(s)}
          </span>
          {s.set ? (
            <span className="text-status-success-fg">Set</span>
          ) : s.required ? (
            <span className="text-status-warning-fg">Not set, required before the release goes live</span>
          ) : (
            <span className="text-tertiary">Not set, optional</span>
          )}
          {onSetSecret && (
            <button
              type="button"
              onClick={() => onSetSecret(s.name)}
              className={`${BUTTON_BASE} ${BUTTON_VARIANT.secondary} ${BUTTON_SIZE.xs}`}
              data-testid={`secret-set-${s.name}`}
            >
              {s.set ? 'Change' : 'Set'}
            </button>
          )}
        </li>
      ))}
    </Group>
  )
}

/** Every block of the manifest in words, one group per block — the
 * "Details" fold of the approval card and of the deploy card. */
export function ManifestBlocks({ app, onSetSecret }: {
  app: PinnedApp
  /** Offered by a host that renders the settings panel: a Set button per secret. */
  onSetSecret?: (name: string) => void
}) {
  const buttons = app.actions.filter((a) => BUTTON_TYPES.includes(a.type))
  const data = app.actions.filter((a) => !BUTTON_TYPES.includes(a.type))
  const files = app.files ?? { read: [], write: [] }
  const folders = files.create ?? []
  const hosts = app.egress ?? []
  const links = app.external?.links ?? []
  const challenge = app.external?.challenge ?? []
  const sessionDays = app.external?.session_days
  const h = app.handlers ?? {}
  const schedules = Object.entries(h.on_schedule ?? {})
  const triggers = h.on_trigger ?? []
  const events = Object.entries(h.on_event ?? {})
  const inbound = Object.entries(app.inbound ?? {})
  const wakes = schedules.length + triggers.length + events.length + inbound.length
  const unattended = wakesPressButtons(app)
  const ex = app.exports ?? {}
  const offers = [
    ...Object.entries(ex.methods ?? {}).map(([name, e]) => ({ kind: 'Answers the call', name, ...e })),
    ...Object.entries(ex.snapshots ?? {}).map(([name, e]) => ({ kind: 'Publishes the snapshot', name, ...e })),
    ...Object.entries(ex.events ?? {}).map(([name, e]) => ({ kind: 'Emits the event', name, ...e })),
  ]
  const placedCalls = Object.keys(ex.methods ?? {})
  const uses = app.bindings ?? []
  const needMcps = neededMcps(app)
  const needProviders = neededProviders(app)
  const steps = Object.entries(app.steps ?? {})

  return (
    <div className="mt-1.5 space-y-1.5" data-testid="manifest-blocks">
      {buttons.length > 0 && (
        <Group heading="Buttons" count={buttons.length}>
          {buttons.map((ac) => <ActionLine key={ac.id} ac={ac} />)}
        </Group>
      )}
      {data.length > 0 && (
        <Group heading="Platform data" count={data.length}>
          {data.map((ac) => <ActionLine key={ac.id} ac={ac} />)}
        </Group>
      )}
      {(files.read.length > 0 || files.write.length > 0) && (
        <Group heading="Files">
          {files.read.length > 0 && <li>Reads files under {codeList(files.read)}</li>}
          {files.write.length > 0 && (
            <li>
              Writes files under {codeList(files.write)}
              {' '}<FloorChip role="contributor" />
            </li>
          )}
          {folders.map((path) => <li key={path}>{CREATES_FOLDER} <Code>{path}</Code></li>)}
        </Group>
      )}
      {(hosts.length > 0 || links.length > 0) && (
        <Group heading="Hosts">
          {hosts.length > 0 && (
            <li>Its server may reach {codeList(hosts)} and DNS. It may also reach other sites that share their address, as on a CDN. It can never reach your local network or the platform.</li>
          )}
          {links.length > 0 && (
            <li data-testid="external-links-line">
              Its page may open {codeList(links)} in a new tab, also on a link
            </li>
          )}
        </Group>
      )}
      {(challenge.length > 0 || (sessionDays !== undefined && sessionDays !== 30)) && (
        <Group heading="On a link">
          {challenge.length > 0 && (
            <li data-testid="external-challenge-line">
              Asks a visitor to pass a bot check before {codeList(challenge)}
            </li>
          )}
          {sessionDays !== undefined && sessionDays !== 30 && (
            <li data-testid="external-session-line">Keeps a visitor’s login for {sessionDays} day{sessionDays === 1 ? '' : 's'}</li>
          )}
        </Group>
      )}
      {wakes > 0 && (
        <Group heading="Wakes" count={wakes}>
          {schedules.map(([name, { cron }]) => (
            <li key={`s-${name}`}>Wakes on the schedule <Code>{cron}</Code> as <Code>{name}</Code></li>
          ))}
          {triggers.map((name) => (
            <li key={`t-${name}`}>Wakes when a trigger aimed at <Code>{name}</Code> fires</li>
          ))}
          {events.map(([name, evs]) => (
            <li key={`e-${name}`}>
              Wakes when {evs.map((e, i) => <span key={e}>{i ? ' or ' : ''}{eventWords(e)}</span>)}, as <Code>{name}</Code>
            </li>
          ))}
          {inbound.map(([name, hook]) => (
            <li key={`i-${name}`} data-testid="inbound-line">
              Receives {inboundWords(hook)} at <Code>/v1/apps/{app.id}/inbound/{name}</Code> as <Code>{hook.handler}</Code>,
              verified with the secret <Code>{hook.secret}</Code>
            </li>
          ))}
          {inbound.length > 0 && (
            <li className="text-tertiary" data-testid="inbound-note">
              An event from outside reaches only the app’s own data and its open pages. It never reaches files, buttons or a chat.
            </li>
          )}
          {unattended && (
            <li className="text-status-warning-fg">Wakes may press the app’s buttons unattended</li>
          )}
        </Group>
      )}
      {steps.length > 0 && (
        <Group heading="Steps" count={steps.length}>
          {steps.map(([name, st]) => (
            <li key={`st-${name}`} data-testid="step-line">
              Runs the script <Code>{st.run}</Code> {stepPlaceWords(app)} when <Code>{name}</Code> wakes.
              {' '}It runs for up to {timeoutWords(st.timeout)}{st.sha256 ? <>, sha256 <Code>{st.sha256.slice(0, 12)}</Code></> : ''}.
            </li>
          ))}
          {needProviders.length > 0 && (
            <li data-testid="step-accounts">
              The scripts receive the {needProviders.map((p) => p.provider).join(', ')} account
              {needProviders.length > 1 ? 's' : ''} of {app.scope === 'shared' ? 'the agent' : 'the owner'}
              {app.scope === 'shared' && (
                <span className="text-status-warning-fg">, and a manager of this agent must approve this</span>
              )}
            </li>
          )}
          {app.has_live_link && (
            <li className="text-status-warning-fg" data-testid="step-link-warning">
              This app runs scripts and is reachable by a link. A visitor’s input is only data to the scripts, never instructions.
            </li>
          )}
        </Group>
      )}
      <SecretsGroup app={app} onSetSecret={onSetSecret} />
      {offers.length > 0 && (
        <Group heading={placedCalls.length ? 'Offers to other apps and to the agents it is placed in' : 'Offers to other apps'} count={offers.length}>
          {offers.map((o) => (
            <li key={`${o.kind}-${o.name}`} className="flex flex-wrap items-baseline gap-x-1.5">
              <span>{o.kind} <Code>{o.name}</Code>: {o.description}</span>
              <FloorChip role={o.min_role} />
            </li>
          ))}
          {placedCalls.length > 0 && (
            <li className="text-tertiary" data-testid="offers-placed-note">
              The calls also answer chats and tasks in agents a share places this app in. They get the role the share gives. The same minimum roles apply there.
            </li>
          )}
        </Group>
      )}
      {uses.length > 0 && (
        <Group heading="Uses other apps" count={uses.length}>
          {uses.map((b) => (
            <li key={b.name}>Calls the app <Code>{b.app}</Code> of agent <Code>{b.agent}</Code> as <Code>{b.name}</Code></li>
          ))}
        </Group>
      )}
      {(needMcps.length > 0 || needProviders.length > 0) && (
        <Group heading="Needs">
          {needMcps.map((m) => (
            <li key={`m-${m.name}`}>
              The MCP <Code>{m.name}</Code>
              {m.assigned === undefined ? '' : m.assigned ? ', assigned to this agent' : (
                <span className="text-status-warning-fg">, not assigned to this agent</span>
              )}
            </li>
          ))}
          {needProviders.map((p) => (
            <li key={`p-${p.provider}`}>
              A connected {p.provider} account{p.mcp ? <> for <Code>{p.mcp}</Code></> : ''}
              {p.connected === undefined ? '' : p.connected ? (
                // The identity the buttons run with (the server's word):
                // the agent's service account for a shared app, the
                // owner's for a personal one.
                p.identity === 'the agent' ? <>, the agent's account{p.account ? <> <Code>{p.account}</Code></> : ''}</>
                  : p.identity === 'the owner' ? <>, the owner's account{p.account ? <> <Code>{p.account}</Code></> : ''}</>
                    : <>, connected{p.account ? <> as <Code>{p.account}</Code></> : ''}</>
              ) : (
                <span className="text-status-warning-fg">
                  {p.identity === 'the agent'
                    ? ', but none is set for this agent. Choose a service account on the agent’s MCPs page.'
                    : p.identity === 'the owner' ? ', but the owner has not connected one' : ', not connected for you'}
                </span>
              )}
            </li>
          ))}
        </Group>
      )}
    </div>
  )
}

/** Only the app's own wakes may press its buttons; an inbound wake never does. */
function wakesPressButtons(app: PinnedApp): boolean {
  const h = app.handlers ?? {}
  const own = Object.keys(h.on_schedule ?? {}).length + (h.on_trigger ?? []).length + Object.keys(h.on_event ?? {}).length
  return own > 0 && app.actions.some((a) => a.type === 'fire_task' || a.type === 'mcp_tool')
}

function neededMcps(app: PinnedApp) {
  return app.requires_status?.mcps ?? (app.requires?.mcps ?? []).map((name) => ({ name, assigned: undefined }))
}

function neededProviders(app: PinnedApp) {
  return app.requires_status?.providers
    ?? (app.requires?.providers ?? []).map((provider) => ({ provider, mcp: '', connected: undefined }))
}

function codeList(items: string[]): ReactNode {
  return items.map((x, i) => <span key={x}>{i ? ', ' : ''}<Code>{x}</Code></span>)
}

/** How many of a list a summary line names before "and N more". */
const FEW = 4

/** A list in prose: "a, b and c", or "a, b, c, d and 3 more". */
function few(items: ReactNode[], max = FEW): ReactNode {
  const shown = items.length > max ? items.slice(0, max) : items
  const rest = items.length - shown.length
  return shown.map((it, i) => (
    <span key={i}>
      {i === 0 ? '' : i === shown.length - 1 && !rest ? ' and ' : ', '}
      {it}
      {i === shown.length - 1 && rest ? ` and ${rest} more` : ''}
    </span>
  ))
}

export interface SummaryLine {
  key: string
  text: ReactNode
  warn?: boolean
  testId?: string
}

/** The plain list: one short line per kind of thing the app may do, in the
 * order of the groups. The sentences with every name, hash and address are
 * the groups behind "Details". */
function summaryLines(app: PinnedApp): SummaryLine[] {
  const out: SummaryLine[] = []
  const buttons = app.actions.filter((a) => BUTTON_TYPES.includes(a.type))
  const data = app.actions.filter((a) => !BUTTON_TYPES.includes(a.type))
  if (buttons.length) {
    out.push({
      key: 'buttons', testId: 'summary-buttons',
      text: <>{buttons.length} button{buttons.length > 1 ? 's' : ''}: {few(buttons.map((a) => a.label))}</>,
    })
  }
  if (data.length) {
    const words = Array.from(new Set(data.map((a) => (
      a.type === 'data_feed' ? `the live ${a.feed} feed` : PLATFORM_METHOD_SHORT[a.method || ''] || a.method || a.label
    ))))
    out.push({ key: 'data', testId: 'summary-data', text: <>Uses the platform for {few(words)}</> })
  }
  const files = app.files ?? { read: [], write: [] }
  if (files.read.length) out.push({ key: 'reads', text: <>Reads files under {few(codeItems(files.read))}</> })
  if (files.write.length) out.push({ key: 'writes', text: <>Writes files under {few(codeItems(files.write))}</> })
  for (const path of files.create ?? []) {
    out.push({ key: `creates-${path}`, testId: 'summary-creates', text: <>{CREATES_FOLDER} <Code>{path}</Code></> })
  }
  if (app.egress?.length) out.push({ key: 'hosts', text: <>Its server may reach {few(codeItems(app.egress))}</> })
  const ext = app.external
  if (ext?.links.length) out.push({ key: 'links', text: <>Its page may open {few(codeItems(ext.links))}</> })
  if (ext?.challenge.length) out.push({ key: 'challenge', text: <>Bot check before {few(codeItems(ext.challenge))}</> })
  if (ext && ext.session_days !== 30) {
    out.push({ key: 'session', text: <>Keeps a visitor’s login for {ext.session_days} day{ext.session_days === 1 ? '' : 's'}</> })
  }
  const h = app.handlers ?? {}
  const wakes: ReactNode[] = [
    ...Object.values(h.on_schedule ?? {}).map(({ cron }) => cronWords(cron)),
    ...(h.on_trigger ?? []).map((name) => <>on a <Code>{name}</Code> trigger</>),
    ...Object.values(h.on_event ?? {}).flat().map((e) => <>when {eventWords(e)}</>),
    ...Object.values(app.inbound ?? {}).map((hook) => <>on {inboundWords(hook)}</>),
  ]
  if (wakes.length) out.push({ key: 'wakes', testId: 'summary-wakes', text: <>Wakes {few(wakes)}</> })
  if (wakesPressButtons(app)) {
    out.push({ key: 'unattended', warn: true, text: 'Its wakes may press its buttons unattended' })
  }
  const steps = Object.entries(app.steps ?? {})
  if (steps.length) {
    out.push({
      key: 'steps', testId: 'summary-steps',
      text: <>Runs the script{steps.length > 1 ? 's' : ''} {few(steps.map(([, st]) => <Code>{st.run}</Code>))} {stepPlaceWords(app)}</>,
    })
    const providers = neededProviders(app)
    if (providers.length) {
      out.push({
        key: 'step-accounts', warn: app.scope === 'shared', testId: 'summary-step-accounts',
        text: <>
          Its scripts receive the {providers.map((p) => p.provider).join(', ')} account{providers.length > 1 ? 's' : ''} of
          {app.scope === 'shared' ? ' the agent, so approval needs a manager' : ' the owner'}
        </>,
      })
    }
    if (app.has_live_link) {
      out.push({
        key: 'step-link', warn: true, testId: 'summary-step-link',
        text: 'Runs scripts and is reachable by a link: a visitor’s input is data, never instructions',
      })
    }
  }
  const secrets = declaredSecrets(app)
  if (secrets.length) {
    const unset = secrets.filter((s) => !s.set && s.required).length
    out.push({
      key: 'secrets', testId: 'summary-secrets', warn: unset > 0,
      text: <>
        Needs the secret{secrets.length > 1 ? 's' : ''} {few(secrets.map((s) => (
          <><Code>{s.name}</Code>{s.set ? ', set' : s.required ? ', not set' : ', optional, not set'}</>
        )))}
      </>,
    })
  }
  const ex = app.exports ?? {}
  const offers: ReactNode[] = [
    ...Object.keys(ex.methods ?? {}).map((n) => <>the <Code>{n}</Code> call</>),
    ...Object.keys(ex.snapshots ?? {}).map((n) => <>the <Code>{n}</Code> snapshot</>),
    ...Object.keys(ex.events ?? {}).map((n) => <>the <Code>{n}</Code> event</>),
  ]
  if (offers.length) out.push({ key: 'offers', text: <>Offers other apps {few(offers)}</> })
  const placedCalls = Object.keys(ex.methods ?? {})
  if (placedCalls.length) {
    out.push({
      key: 'placed-calls', testId: 'summary-placed-calls',
      text: <>Answers the agents it is placed in with {few(placedCalls.map((n) => <>the <Code>{n}</Code> call</>))}</>,
    })
  }
  const uses = app.bindings ?? []
  if (uses.length) {
    out.push({ key: 'uses', text: <>Uses {few(uses.map((b) => <>the <Code>{b.app}</Code> app of agent <Code>{b.agent}</Code></>))}</> })
  }
  // One line for the MCPs an app needs (a template app lists every tool
  // its cards describe — seven lines said the same thing on a phone), the
  // missing ones named; a single MCP keeps its own line.
  const mcps = neededMcps(app)
  if (mcps.length > 1) {
    const missing = mcps.filter((m) => m.assigned === false)
    const judged = mcps.some((m) => m.assigned !== undefined)
    out.push({
      key: 'mcps', testId: 'summary-mcps', warn: missing.length > 0,
      text: <>
        Needs the MCPs {few(codeItems(mcps.map((m) => m.name)))}
        {!judged ? '' : missing.length
          ? <>. Not assigned to this agent: {few(codeItems(missing.map((m) => m.name)))}</>
          : ', all assigned'}
      </>,
    })
  } else {
    for (const m of mcps) {
      out.push({
        key: `mcp-${m.name}`, testId: `summary-mcp-${m.name}`, warn: m.assigned === false,
        text: <>Needs the MCP <Code>{m.name}</Code>{m.assigned === undefined ? '' : m.assigned ? ', assigned' : ', not assigned to this agent'}</>,
      })
    }
  }
  for (const p of neededProviders(app)) {
    const who = p.connected === undefined ? ''
      : p.connected
        ? p.identity === 'the agent' ? ', the agent’s own' : p.identity === 'the owner' ? ', the owner’s own' : ', connected for you'
        : p.identity === 'the agent' ? ', but none is set for this agent'
          : p.identity === 'the owner' ? ', but the owner has not connected one' : ', not connected for you'
    out.push({
      key: `provider-${p.provider}`, testId: `summary-provider-${p.provider}`, warn: p.connected === false,
      text: <>Needs a {p.provider} account{who}</>,
    })
  }
  return out
}

function codeItems(items: string[]): ReactNode[] {
  return items.map((x) => <Code>{x}</Code>)
}

/** The plain list the card's body opens with, with one Set button on the
 * secrets line when the host offers the settings panel. `before` lets the
 * deploy card put its own first line (the files it changes) on top; `only`
 * keeps the named lines alone (the deploy card waiting on a secret of an
 * approved manifest says the secrets, nothing else again). */
export function ManifestSummary({ app, onSetSecret, before, only }: {
  app: PinnedApp; onSetSecret?: () => void; before?: SummaryLine[]; only?: string[]
}) {
  const own = summaryLines(app).filter((l) => !only || only.includes(l.key))
  const lines = [...(before ?? []), ...own]
  if (!lines.length) return null
  return (
    <ul className="mt-1.5 space-y-1 text-secondary" data-testid="manifest-summary">
      {lines.map((l) => (
        <li
          key={l.key}
          className={`flex flex-wrap items-baseline gap-x-1.5 gap-y-0.5 ${l.warn ? 'text-status-warning-fg' : ''}`}
          data-testid={l.testId}
        >
          <span>{l.text}</span>
          {l.key === 'secrets' && onSetSecret && (
            <button
              type="button"
              onClick={onSetSecret}
              className={`${BUTTON_BASE} ${BUTTON_VARIANT.secondary} ${BUTTON_SIZE.xs}`}
              data-testid="summary-secret-set"
            >
              Set
            </button>
          )}
        </li>
      ))}
    </ul>
  )
}

/** The "Details" fold's toggle: the groups with every sentence. Lives in
 * the pinned footer so it stays reachable however long the body is. */
export function DetailsToggle({ open, onToggle }: { open: boolean; onToggle: () => void }) {
  return (
    <button
      type="button"
      onClick={onToggle}
      className="text-secondary underline decoration-dotted underline-offset-2 hover:text-primary"
      data-testid="details-toggle"
    >
      {open ? 'Hide details' : 'Details'}
    </button>
  )
}

/** The exact-manifest expander's toggle and its text. The `<pre>` scrolls
 * with the body it sits in; only long lines scroll sideways. */
export function ExactManifestToggle({ open, onToggle }: { open: boolean; onToggle: () => void }) {
  return (
    <button
      type="button"
      onClick={onToggle}
      className="text-tertiary underline decoration-dotted underline-offset-2 hover:text-secondary"
      data-testid="exact-manifest-toggle"
    >
      {open ? 'Hide declaration' : 'Exact declaration'}
    </button>
  )
}

export function ExactManifest({ app }: { app: PinnedApp }) {
  return (
    <pre className="mt-1 overflow-x-auto rounded-card bg-surface-sunken p-2 font-mono text-3xs leading-snug text-secondary" data-testid="exact-manifest">
      {JSON.stringify(exactManifest(app), null, 2)}
    </pre>
  )
}

/** The card's shell: a warning-tinted box that is a shrinkable flex column
 * of the column it sits in (`min-h-0 shrink`), with a pinned header, a body that
 * scrolls when the card is clamped, and a pinned footer. Used by the
 * approval card and the deploy card so the two behave the same on every
 * mount point. */
export function AppCardShell({ header, body, footer, testId }: {
  header: ReactNode; body: ReactNode; footer: ReactNode; testId: string
}) {
  return (
    <div
      className="mx-3 mt-2 flex min-h-0 shrink flex-col rounded-card border border-status-warning-border bg-status-warning-bg px-3 py-2.5 text-xs"
      data-testid={testId}
    >
      <div className="shrink-0" data-testid="app-card-header">{header}</div>
      <div className="min-h-0 flex-1 overflow-y-auto" data-testid="app-card-body">{body}</div>
      <div className="mt-2 flex shrink-0 flex-wrap items-center gap-2" data-testid="app-card-footer">{footer}</div>
    </div>
  )
}

interface Props {
  app: PinnedApp
  /** The agent whose ['apps', agent] cache the approval refreshes — for Dock
      pins use the pin row's own agent (a project pin may be foreign). */
  agent: string
}

export default function AppApprovalCard({ app, agent }: Props) {
  const approve = useApproveApp(agent)
  const [showManifest, setShowManifest] = useState(false)
  const [details, setDetails] = useState(false)
  // The settings panel (APPS.md "Secrets"), opened from a Set button: the
  // card owns it, so every host that mounts the card offers it.
  const [settings, setSettings] = useState(false)
  const canSet = appKind(app).hasSettings && app.can_manage
  const onSetSecret = canSet ? () => setSettings(true) : undefined
  const n = app.actions.length
  const onlyButtons = Object.keys(exactManifest(app)).length === 1
  const headline = app.approval_stale
    ? 'The approval of this app’s actions no longer holds. Review and approve them again.'
    : onlyButtons && n > 0
      ? `“${app.title || app.slug}” declares ${n} action button${n > 1 ? 's' : ''}. Review ${n > 1 ? 'them' : 'it'} before use.`
      : `“${app.title || app.slug}” declares what it may do. Review it before it works.`

  return (
    <AppCardShell
      testId="app-approval-card"
      header={<p className="font-medium text-primary">{headline}</p>}
      body={(
        <>
          <ManifestSummary app={app} onSetSecret={onSetSecret} />
          {details && <ManifestBlocks app={app} onSetSecret={onSetSecret} />}
          {showManifest && <ExactManifest app={app} />}
          {settings && <AppSettingsPanel app={app} agent={agent} onClose={() => setSettings(false)} />}
        </>
      )}
      footer={(
        <>
          {app.can_approve ? (
            <Button
              variant="primary"
              size="xs"
              onClick={() => approve.mutate({ appId: app.id, sig: app.actions_sig })}
              disabled={approve.isPending}
            >
              Approve
            </Button>
          ) : (
            <span className="text-tertiary">
              {app.steps_need_manager
                ? 'Approval needs a manager of this agent: its scripts receive the agent’s accounts.'
                : app.scope === 'shared'
                  ? 'Approval needs an editor of this agent with run access to the tasks.'
                  : 'Approval needs run access to the referenced tasks.'}
            </span>
          )}
          <DetailsToggle open={details} onToggle={() => setDetails((v) => !v)} />
          <ExactManifestToggle open={showManifest} onToggle={() => setShowManifest((v) => !v)} />
          {approve.isError && (
            <span className="text-status-danger-fg">{(approve.error as Error).message}</span>
          )}
        </>
      )}
    />
  )
}
