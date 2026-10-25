import { useEffect, useMemo, useState } from 'react'
import type { PinnedApp } from '../../api/apps'
import {
  ConfirmRequired,
  GRANTEE_KIND,
  SHARE_DECISION,
  SHARE_SCOPE,
  SHARE_TARGET,
  useCreateShare,
  usePatchShare,
  useShares,
  useSharingSettings,
  useUserDirectory,
  type CreateShareResult,
  type GranteeKind,
  type Share,
} from '../../api/shares'
import { passkeyConfirm } from '../../api/webauthn'
import { startOidcConfirm } from '../../api/auth'
import { useAuth } from '../../contexts/AuthContext'
import { formatExpiry } from '../../lib/format'
import { AGENT_ROLES, ROLE, actingRole, isAdmin, rank, roleLabel, type AgentRole } from '../../lib/permissions'
import { peekConfirm, peekPending, savePending, takeConfirm, takePending, type PendingShare } from '../../lib/shareConfirm'
import Dialog from '../ui/Dialog'
import { SaveState } from '../ui/SettingsControls'
import { useSaveState } from '../../hooks/useSaveState'
import Button from '../ui/Button'
import { DIALOG_PANEL } from '../ui/classes'

/**
 * The share popover (SHARING.md): who an app is shared with and how to add
 * someone. One component for the app menu and the full-screen page; the
 * chat-history row reuses it with a chat target. "People" shares with a
 * person (who accepts the app into one of their agents), an agent (its
 * editors and managers place it in that agent's Apps panel) or, for an
 * admin, a department; a role cap says what the recipients act as. "Link"
 * makes a link for someone without an account (password by default; making
 * one, or turning its Buttons on, asks for a confirm of the person at the
 * keyboard).
 */

/** What is being shared: an app row, or a chat (shared as a snapshot). */
export interface ShareTarget {
  kind: 'app' | 'chat'
  id: string
  title: string
  slug?: string
  /** Apps: who sees it by default; chats: 'personal' (per-user) or 'shared' (a shared-only agent's). */
  scope?: 'shared' | 'personal'
  /** The target's own agent (left out of the agents a share may name). */
  agent?: string
  actions?: PinnedApp['actions']
  /** The app runs scripts on its own (APPS.md "Steps"): the link form says
   * that a visitor's input is data to them, never instructions. */
  has_steps?: boolean
}

interface Props {
  /** An app row (the menu and the full-screen page pass the row itself). */
  app?: Pick<PinnedApp, 'id' | 'title' | 'slug' | 'scope' | 'pin_scope'> & { agent?: string; actions?: PinnedApp['actions']; steps?: PinnedApp['steps'] }
  /** Or any target; wins over `app` when both are given. */
  target?: ShareTarget
  onClose: () => void
  /** The tab to open on: the identity-provider round trip comes back on Link. */
  initialTab?: Tab
  /** Why that round trip failed, to show in place of the link form's notice. */
  confirmError?: string
}

type Tab = 'people' | 'link'
type ConfirmExtra = { password?: string; confirm_token?: string }
/** What a share write is, for the identity-provider round trip to keep. */
type PendingDescriptor = Omit<PendingShare, 'at' | 'return_to'>

type ExpiryChoice = { value: string; label: string }
const DAY_CHOICES = [7, 30, 90]
const NEVER_CHOICE: ExpiryChoice = { value: 'never', label: 'Never expires' }

/** The expiry choices a share form offers under the admin's cap (days;
 * `null` when nothing caps it, `undefined` while the settings have not
 * answered): the durations up to the cap, the cap itself added when it is
 * not one of them, and "Never expires" only while nothing caps it (first
 * for people, last for a link, whose default stays a duration so a forever
 * link is a choice). The server refuses anything past the cap, so the form
 * never offers it; the cap's own choice says it is the longest allowed. */
function expiryChoices(capDays: number | null | undefined, form: 'people' | 'link'): ExpiryChoice[] {
  const days = capDays
    ? [...new Set([...DAY_CHOICES.filter((d) => d <= capDays), capDays])].sort((a, b) => a - b)
    : DAY_CHOICES
  const durations = days.map((d) => ({
    value: `${d}d`,
    label: `Expires in ${d} day${d === 1 ? '' : 's'}${d === capDays ? ', the longest allowed' : ''}`,
  }))
  if (capDays !== null) return durations
  return form === 'people' ? [NEVER_CHOICE, ...durations] : [...durations, NEVER_CHOICE]
}

/** What a form shows selected: the person's pick while it is still offered,
 * else the default: Never for people with no cap, 30 days for a link, and
 * under a cap the longest it allows. */
function pickExpiry(choices: ExpiryChoice[], picked: string, form: 'people' | 'link'): string {
  if (choices.some((c) => c.value === picked)) return picked
  if (form === 'people' && choices[0]?.value === NEVER_CHOICE.value) return NEVER_CHOICE.value
  if (choices.some((c) => c.value === '30d') && form === 'link') return '30d'
  const durations = choices.filter((c) => c.value !== NEVER_CHOICE.value)
  return durations[durations.length - 1]?.value ?? ''
}

// The role words from the weakest up: the cap select reads top to bottom.
const CAPS_ASCENDING: readonly AgentRole[] = [...AGENT_ROLES].reverse()

const expiryLabel = formatExpiry

const errorText = (e: unknown) => (e instanceof Error ? e.message : String(e))

/** A personal app's scope word, typed to the row's own union. */
const PERSONAL_SCOPE: PinnedApp['scope'] = 'personal'

/** What the row says about a share's standing: a person's answer, a pause. */
function shareState(s: Share): string {
  if (s.state === 'suspended') return 'paused while the app is unpinned'
  if (s.decision === SHARE_DECISION.DECLINED) return s.decided_by_name ? `declined by ${s.decided_by_name}` : 'declined'
  if (s.decision === SHARE_DECISION.PENDING && s.target_kind === SHARE_TARGET.APP) return 'waiting to be accepted'
  const accepted = s.decision === SHARE_DECISION.ACCEPTED && s.target_kind === SHARE_TARGET.APP && s.grantee ? 'accepted' : ''
  return [accepted, expiryLabel(s.expires_at)].filter(Boolean).join(' · ')
}

/** The notice after a share landed, by what it did. */
function madeNotice(share: Share, typed: string): string {
  if (share.to_department) return `Shared with the ${share.to_department.name || 'department'} department.`
  if (share.to_agent) return `Added to ${share.to_agent.name}'s apps.`
  const who = share.grantee?.name || share.grantee?.username || typed
  if (share.target_kind === SHARE_TARGET.APP) return `Waiting for ${who} to accept.`
  return `Shared with ${who}.`
}

export default function SharePopover({ app: appProp, target: targetProp, onClose, initialTab, confirmError }: Props) {
  const target: ShareTarget = targetProp ?? {
    kind: 'app', id: appProp!.id, title: appProp!.title, slug: appProp!.slug,
    scope: appProp!.scope, agent: appProp!.agent, actions: appProp!.actions,
    has_steps: Object.keys(appProp!.steps ?? {}).length > 0,
  }
  const isChat = target.kind === SHARE_TARGET.CHAT
  const hasSteps = !!target.has_steps
  // The popover reads `app` below as the target's row-like view.
  const app = { id: target.id, title: target.title, slug: target.slug || '', scope: target.scope, actions: target.actions }
  const { data: shares, isLoading, error: loadError } = useShares(target.kind, target.id)
  const { data: directory } = useUserDirectory()
  const { data: sharingSettings, isError: settingsFailed } = useSharingSettings()
  // The admin's cap; when the settings could not be read the server judges.
  const capDays = sharingSettings ? sharingSettings.max_expiry_days : settingsFailed ? null : undefined
  const settingsKnown = capDays !== undefined
  const peopleExpiryChoices = expiryChoices(capDays, 'people')
  const linkExpiryChoices = expiryChoices(capDays, 'link')
  const create = useCreateShare()
  const patch = usePatchShare()
  // The Buttons box saves through the step-up flow, so its indication is
  // set once the write lands, on the first try or the retry.
  const { stateOf, markSaved } = useSaveState()
  const user = useAuth()?.user ?? null
  // The account the identity-provider round trip files its entries under.
  const sub = user?.sub ?? ''
  // Back from the identity provider (SHARING.md "The confirm"): the choices
  // come back as they were, the link tab opens, and the token arms the
  // button. They are the form's first state; the entries are taken once
  // the popover is up. Nothing fires without a click.
  const [restored] = useState(() => {
    const pending = peekPending(sub)
    if (!pending || pending.app_id !== target.id) return null
    const token = confirmError ? null : peekConfirm(sub)
    return { pending, token }
  })
  useEffect(() => {
    if (!restored) return
    takePending(sub, target.id)
    takeConfirm(sub)
  }, [restored, sub, target.id])
  const [includeTools, setIncludeTools] = useState(restored?.pending.fields.include_tools ?? false)
  const [tab, setTab] = useState<Tab>(restored ? 'link' : initialTab ?? 'people')
  const [who, setWho] = useState('')
  const [kind, setKind] = useState<GranteeKind>(GRANTEE_KIND.PERSON)
  const [agentPick, setAgentPick] = useState('')
  const [deptPick, setDeptPick] = useState('')
  const [cap, setCap] = useState<AgentRole>(ROLE.VIEWER)
  const [expiresPick, setExpiresIn] = useState('')
  const [notice, setNotice] = useState(() => {
    if (!restored?.token) return ''
    return restored.pending.op === 'create' ? 'Confirmed. Make the link now.' : 'Confirmed. Turn the buttons on now.'
  })
  const [error, setError] = useState(() => {
    if (!restored) return ''
    if (confirmError) return confirmError
    return restored.token ? '' : 'Your confirmation took too long. Make the link again.'
  })
  const [copied, setCopied] = useState('')
  // Link form.
  const [publicLink, setPublicLink] = useState(restored?.pending.fields.public ?? false)
  const [publicUnderstood, setPublicUnderstood] = useState(restored?.pending.fields.public ?? false)
  const [linkExpiryPick, setLinkExpiry] = useState(restored?.pending.fields.expiry ?? '30d')
  // What the two selects show and send: the pick while the cap allows it.
  const expiresIn = pickExpiry(peopleExpiryChoices, expiresPick, 'people')
  const linkExpiry = pickExpiry(linkExpiryChoices, linkExpiryPick, 'link')
  const [linkButtons, setLinkButtons] = useState(restored?.pending.fields.buttons ?? false)
  const [made, setMade] = useState<CreateShareResult | null>(null)
  // The confirm the server asked for, and what to run once given.
  const [confirmAsk, setConfirmAsk] = useState<{
    method: 'password' | 'passkey' | 'oidc' | 'none'
    retry: (extra: ConfirmExtra) => Promise<void>
    /** The identity-provider method: the provider's name and the write to keep. */
    provider?: string
    pending?: PendingDescriptor
  } | null>(null)
  const [confirmPassword, setConfirmPassword] = useState('')
  // A confirm token the identity-provider round trip brought back, armed
  // for the write it was asked for; the next matching click spends it.
  const [armed, setArmed] = useState<{ token: string; op: 'create' | 'patch'; share_id?: string } | null>(
    () => (restored?.token
      ? { token: restored.token, op: restored.pending.op, share_id: restored.pending.share_id }
      : null),
  )

  const title = app.title || app.slug
  const link = `${typeof window !== 'undefined' ? window.location.origin : ''}/apps/${app.id}`
  const internal = useMemo(() => (shares ?? []).filter((s) => s.scope === SHARE_SCOPE.INTERNAL), [shares])
  const external = useMemo(() => (shares ?? []).filter((s) => s.scope === SHARE_SCOPE.EXTERNAL), [shares])
  // The people already holding a live share (a declined one may be shared again).
  const granted = useMemo(
    () => new Set(internal.filter((s) => s.grantee && s.decision !== SHARE_DECISION.DECLINED).map((s) => s.grantee?.sub)),
    [internal],
  )
  const people = directory?.users ?? null
  const candidates = useMemo(
    () => (people ?? []).filter((u) => !granted.has(u.sub)),
    [people, granted],
  )
  // Agents and departments a share may name (the server lists them per the
  // admin's switches and the directory); never for a chat or a personal
  // app, which go to people only.
  const peopleOnly = isChat || app.scope === PERSONAL_SCOPE
  // Only an agent the sharer edits or manages can take the app (the server
  // refuses the rest with its reason).
  const agentChoices = useMemo(
    () => (peopleOnly ? [] : (directory?.agents ?? []).filter((a) =>
      a.slug !== target.agent && (isAdmin(user) || rank(actingRole(user, a.slug)) >= rank(ROLE.EDITOR)))),
    [directory, peopleOnly, target.agent, user],
  )
  const deptChoices = peopleOnly ? [] : (directory?.departments ?? [])
  const kinds: GranteeKind[] = [
    GRANTEE_KIND.PERSON,
    ...(agentChoices.length ? [GRANTEE_KIND.AGENT] : []),
    ...(deptChoices.length ? [GRANTEE_KIND.DEPARTMENT] : []),
  ]
  const effectiveKind: GranteeKind = kinds.includes(kind) ? kind : GRANTEE_KIND.PERSON
  // The caps offered: up to the sharer's own standing on the app (the
  // owner of a personal app is its manager); never on a chat (a copy is
  // read-only).
  const myRole = isAdmin(user) ? ROLE.ADMIN : app.scope === PERSONAL_SCOPE ? ROLE.MANAGER : actingRole(user, target.agent ?? '')
  const capChoices = isChat ? [] : CAPS_ASCENDING.filter((r) => rank(r) <= rank(myRole))
  const platformActions = (app.actions ?? []).filter((a) => a.type === 'mcp_tool' || a.type === 'fire_task')

  // Where the identity-provider round trip comes back to: an app's own page
  // opens this popover from the URL; a chat's page does not, so the person
  // opens the share again and finds it armed.
  const returnTo = target.kind === SHARE_TARGET.APP
    ? `/apps/${target.id}?share=1&tab=link`
    : `${window.location.pathname}${window.location.search}`

  /** Run a share write; when the server asks for a confirm, gather
   * it (a password field, the passkey ceremony, or the round trip to the
   * identity provider) and run again. `describe` says what the write is,
   * so the round trip can keep it and a token brought back can spend
   * itself on the matching click. */
  const withConfirm = async (run: (extra: ConfirmExtra) => Promise<void>, describe?: () => PendingDescriptor) => {
    setError('')
    const desc = describe?.()
    let first: ConfirmExtra = {}
    if (armed && desc && armed.op === desc.op && (armed.share_id ?? '') === (desc.share_id ?? '')) {
      first = { confirm_token: armed.token }
      setArmed(null)
      setNotice('')
    }
    try {
      await run(first)
    } catch (e) {
      if (!(e instanceof ConfirmRequired)) return setError(errorText(e))
      if (e.method === 'passkey') {
        try {
          const token = await passkeyConfirm()
          await run({ confirm_token: token })
        } catch (err) {
          setError(errorText(err))
        }
        return
      }
      if (e.method === 'oidc') {
        setConfirmAsk({ method: 'oidc', provider: e.provider || 'your identity provider', pending: desc, retry: async () => {} })
        return
      }
      setConfirmAsk({
        method: e.method,
        retry: async (extra) => {
          try {
            await run(extra)
            setConfirmAsk(null)
            setConfirmPassword('')
          } catch (err) {
            setError(errorText(err))
          }
        },
      })
    }
  }

  /** Leave for the provider with the write kept for the return. */
  const continueToProvider = () => {
    if (confirmAsk?.pending) savePending(sub, { ...confirmAsk.pending, return_to: returnTo })
    startOidcConfirm(returnTo).catch((err) => setError(errorText(err)))
  }

  const typedPerson = who.trim()
  const grantee = effectiveKind === GRANTEE_KIND.PERSON
    // A directory pick resolves to the sub; anything else goes as typed
    // (an exact username or email, the only way with the directory closed).
    ? (candidates.find((u) => u.name === typedPerson || u.username === typedPerson || u.sub === typedPerson)?.sub ?? typedPerson)
    : effectiveKind === GRANTEE_KIND.AGENT ? agentPick : deptPick
  const canSubmit = !!grantee && !create.isPending && settingsKnown

  const submitShare = (e: React.FormEvent) => {
    e.preventDefault()
    if (!grantee) return
    setNotice('')
    setError('')
    create.mutate(
      { target_kind: target.kind, target_id: app.id, grantee_kind: effectiveKind, grantee, expires_in: expiresIn,
        ...(isChat ? { include_tools: includeTools } : { role_cap: cap }) },
      {
        onSuccess: (body) => {
          setWho('')
          setNotice(body.share
            ? madeNotice(body.share, typedPerson)
            : effectiveKind === GRANTEE_KIND.PERSON
              ? 'Done. If that user exists, they will find it under Shared with you.'
              : 'Done. If that agent exists, its members now have the app.')
        },
        onError: (err) => setError(errorText(err)),
      },
    )
  }

  const submitLink = (e: React.FormEvent) => {
    e.preventDefault()
    if (publicLink && !publicUnderstood) return
    void withConfirm(async (extra) => {
      const body = await create.mutateAsync({
        target_kind: target.kind, target_id: app.id, scope: 'external',
        public: publicLink, allow_actions: !isChat && linkButtons, expires_in: linkExpiry,
        ...(isChat ? { include_tools: includeTools } : {}), ...extra,
      })
      setMade(body)
    }, () => ({
      app_id: app.id, target_kind: target.kind, op: 'create', tab: 'link',
      fields: { public: publicLink, expiry: linkExpiry, buttons: !isChat && linkButtons, include_tools: includeTools },
    }))
  }

  const toggleButtons = (s: Share) => {
    void withConfirm(async (extra) => {
      await patch.mutateAsync({ id: s.id, allow_actions: !s.allow_actions, ...extra })
      markSaved(`buttons:${s.id}`)
    }, () => ({
      app_id: app.id, target_kind: target.kind, op: 'patch', share_id: s.id, tab: 'link',
      fields: { public: false, expiry: linkExpiry, buttons: false, include_tools: includeTools, allow_actions: !s.allow_actions },
    }))
  }

  const failed = (e: unknown) => setError(e instanceof Error ? e.message : 'The change did not go through')

  const copy = async (text: string, what: string) => {
    try {
      await navigator.clipboard.writeText(text)
      setCopied(what)
      setTimeout(() => setCopied(''), 1500)
    } catch { /* clipboard unavailable: the text is selectable */ }
  }

  const shareRow = (s: Share) => {
    const name = s.grantee?.name || s.grantee?.username || s.to_agent?.name || s.to_department?.name || 'someone'
    const prefix = s.to_agent ? 'Agent: ' : s.to_department ? 'Department: ' : ''
    const state = shareState(s)
    const capWord = !isChat && s.role_cap !== ROLE.VIEWER ? roleLabel(s.role_cap).toLowerCase() : ''
    return (
      <li key={s.id} className="flex items-center gap-2 py-1.5">
        <div className="min-w-0 flex-1">
          <p className="truncate text-sm text-primary">
            {prefix}{name}
            {capWord && <span className="ml-1 rounded-sm bg-surface-muted px-1 py-px text-3xs text-secondary">{capWord}</span>}
          </p>
          {state && <p className="text-2xs text-tertiary">{state}</p>}
        </div>
        {s.state === 'suspended' && (
          <Button
            type="button"
            onClick={() => patch.mutate({ id: s.id, resume: true }, { onError: failed })}
          >
            Resume
          </Button>
        )}
        <button type="button" onClick={() => patch.mutate({ id: s.id, revoke: true }, { onError: failed })}
          className="rounded-control px-2 py-0.5 text-2xs text-status-danger-fg hover:bg-status-danger-bg"
          aria-label={`Remove ${name}`}>
          Remove
        </button>
      </li>
    )
  }

  const linkRow = (s: Share) => {
    const opened = s.access_count ? `opened ${s.access_count}×` : 'not opened yet'
    const bits = [s.public ? 'public' : 'password', opened, expiryLabel(s.expires_at)].filter(Boolean)
    return (
      <li key={s.id} className="flex items-center gap-2 py-1.5">
        <div className="min-w-0 flex-1">
          <p className="truncate text-sm text-primary">
            Link
            {s.public && <span className="ml-1 rounded-sm bg-status-warning-bg px-1 py-px text-3xs text-status-warning-fg">public</span>}
            {s.state === 'suspended' && <span className="ml-1 text-3xs text-status-warning-fg">paused</span>}
          </p>
          <p className="text-2xs text-tertiary">{bits.join(' · ')}</p>
        </div>
        {platformActions.length > 0 && (
          <label className="flex items-center gap-1 text-2xs text-secondary" title="Platform buttons on this link">
            <input type="checkbox" checked={s.allow_actions} onChange={() => toggleButtons(s)} aria-label={`Buttons on link ${s.id}`} />
            Buttons
          </label>
        )}
        {platformActions.length > 0 && <SaveState state={stateOf(`buttons:${s.id}`)} />}
        <button type="button" onClick={() => patch.mutate({ id: s.id, revoke: true }, { onError: failed })}
          className="rounded-control px-2 py-0.5 text-2xs text-status-danger-fg hover:bg-status-danger-bg"
          aria-label={`Revoke link ${s.id}`}>
          Revoke
        </button>
      </li>
    )
  }

  const tabBtn = (t: Tab, label: string) => (
    <button type="button" onClick={() => { setTab(t); setError(''); setNotice('') }}
      className={`flex-1 rounded-sm px-2 py-1 text-center font-medium ${tab === t ? 'bg-action-subtle text-link' : 'text-secondary hover:text-primary'}`}>
      {label}
    </button>
  )

  const subtitle = isChat
    ? 'A read-only copy of the conversation as it stands now. Share again to send a newer one.'
    : app.scope === 'shared'
      ? 'Members of this agent already see it. Add a person, place it in another agent, or make a link.'
      : 'Only you see this app. Add a colleague, or make a link.'

  const kindLabel = (k: GranteeKind) => k === GRANTEE_KIND.PERSON ? 'A person' : k === GRANTEE_KIND.AGENT ? 'An agent' : 'A department'
  const selectClass = 'min-w-0 flex-1 rounded-control border border-strong bg-surface-card px-2 py-1.5 text-sm text-primary focus:border-action focus:outline-none'

  // A made link's password and the confirm step must not go with a stray
  // click on the backdrop; Escape and Close still close.
  const keep = !!made || !!confirmAsk
  return (
    <Dialog
      onClose={onClose}
      placement="sheet"
      dismissible={keep ? 'escape' : 'any'}
      aria-label={`Share ${title}`}
      panelClassName={`${DIALOG_PANEL} max-h-[90dvh] max-w-md overflow-y-auto p-4`}
    >
        <div className="flex items-start gap-2">
          <div className="min-w-0 flex-1">
            <h2 className="truncate text-sm font-semibold">Share “{title}”</h2>
            <p className="text-2xs text-tertiary">{subtitle}</p>
          </div>
          <button type="button" onClick={onClose} aria-label="Close"
            className="flex h-7 w-7 items-center justify-center rounded-full text-secondary hover:bg-surface-hover hover:text-primary">
            <svg className="h-4 w-4" fill="none" stroke="currentColor" viewBox="0 0 24 24" strokeWidth={2}>
              <path strokeLinecap="round" strokeLinejoin="round" d="M6 18L18 6M6 6l12 12" />
            </svg>
          </button>
        </div>

        <div className="mt-3 flex items-center gap-1 rounded-control border border-default p-0.5 text-xs">
          {tabBtn('people', 'People')}
          {tabBtn('link', 'Link')}
        </div>
        {isChat && (
          <label className="mt-2 flex items-center gap-1.5 text-2xs text-secondary">
            <input type="checkbox" checked={includeTools} onChange={(e) => setIncludeTools(e.target.checked)} aria-label="Include tool calls" />
            Include the tool calls and thinking blocks
          </label>
        )}

        {tab === 'people' && (
          <>
            <form onSubmit={submitShare} className="mt-3 flex flex-col gap-2">
              {kinds.length > 1 && (
                <select value={effectiveKind} onChange={(e) => setKind(e.target.value as GranteeKind)} aria-label="Share with"
                  className={selectClass}>
                  {kinds.map((k) => <option key={k} value={k}>{kindLabel(k)}</option>)}
                </select>
              )}
              <div className="flex gap-2">
                {effectiveKind === GRANTEE_KIND.PERSON && (
                  <>
                    <input list={people ? `share-directory-${app.id}` : undefined} value={who}
                      onChange={(e) => setWho(e.target.value)}
                      placeholder={people ? 'Name or username' : 'Exact username or email'}
                      aria-label="Who to share with"
                      className="min-w-0 flex-1 rounded-control border border-strong bg-surface-card px-2 py-1.5 text-sm text-primary placeholder:text-tertiary focus:border-action focus:outline-none" />
                    {people && (
                      <datalist id={`share-directory-${app.id}`}>
                        {candidates.map((u) => <option key={u.sub} value={u.name || u.username}>{u.username}</option>)}
                      </datalist>
                    )}
                  </>
                )}
                {effectiveKind === GRANTEE_KIND.AGENT && (
                  <select value={agentPick} onChange={(e) => setAgentPick(e.target.value)} aria-label="Which agent" className={selectClass}>
                    <option value="">Choose an agent</option>
                    {agentChoices.map((a) => <option key={a.slug} value={a.slug}>{a.display_name || a.slug}</option>)}
                  </select>
                )}
                {effectiveKind === GRANTEE_KIND.DEPARTMENT && (
                  <select value={deptPick} onChange={(e) => setDeptPick(e.target.value)} aria-label="Which department" className={selectClass}>
                    <option value="">Choose a department</option>
                    {deptChoices.map((d) => <option key={d.id} value={d.id}>{d.name || d.id}</option>)}
                  </select>
                )}
                <select value={expiresIn} onChange={(e) => setExpiresIn(e.target.value)} aria-label="Expiry"
                  className="rounded-control border border-strong bg-surface-card px-2 py-1.5 text-xs text-secondary focus:border-action focus:outline-none">
                  {peopleExpiryChoices.map((c) => <option key={c.value} value={c.value}>{c.label}</option>)}
                </select>
              </div>
              {capChoices.length > 0 && (
                <label className="flex items-center gap-2 text-2xs text-secondary">
                  <span className="shrink-0">They act as</span>
                  <select value={capChoices.includes(cap) ? cap : ROLE.VIEWER} onChange={(e) => setCap(e.target.value as AgentRole)} aria-label="Role"
                    className="min-w-0 flex-1 rounded-control border border-strong bg-surface-card px-2 py-1 text-xs text-primary focus:border-action focus:outline-none">
                    {capChoices.map((r) => <option key={r} value={r}>{roleLabel(r)}</option>)}
                  </select>
                </label>
              )}
              <div className="flex items-center gap-2">
                <Button variant="primary" size="xs" type="submit" disabled={!canSubmit}>
                  Share
                </Button>
                {notice && <span className="text-2xs text-secondary">{notice}</span>}
              </div>
            </form>
            {!isChat && (
              <>
                <div className="mt-3 flex items-center gap-2 border-t border-subtle pt-2">
                  <code className="min-w-0 flex-1 truncate rounded-sm bg-surface-sunken px-2 py-1 font-mono text-2xs text-secondary" title={link}>{link}</code>
                  <Button type="button" onClick={() => copy(link, 'link')}>
                    {copied === 'link' ? 'Copied' : 'Copy link'}
                  </Button>
                </div>
                <p className="mt-1 text-3xs text-tertiary">The link opens for members and for everyone above after they sign in.</p>
              </>
            )}
          </>
        )}

        {tab === 'link' && (
          <>
            {made ? (
              <div className="mt-3 rounded-card border border-status-success-border bg-status-success-bg p-3 text-xs">
                <p className="font-medium text-primary">Your link is ready. The password is shown only now.</p>
                <div className="mt-2 flex items-center gap-2">
                  <code className="min-w-0 flex-1 truncate rounded-sm bg-surface-sunken px-2 py-1 font-mono text-2xs" title={made.link}>{made.link}</code>
                  <Button type="button" onClick={() => copy(made.link || '', 'made-link')}>
                    {copied === 'made-link' ? 'Copied' : 'Copy link'}
                  </Button>
                </div>
                {made.password && (
                  <div className="mt-2 flex items-center gap-2">
                    <span className="text-tertiary">Password</span>
                    <code className="rounded-sm bg-surface-sunken px-2 py-1 font-mono text-xs tracking-wider">{made.password}</code>
                    <Button type="button" onClick={() => copy(made.password || '', 'password')}>
                      {copied === 'password' ? 'Copied' : 'Copy'}
                    </Button>
                  </div>
                )}
                <button type="button" onClick={() => setMade(null)} className="mt-2 text-2xs text-secondary underline decoration-dotted">
                  Make another link
                </button>
              </div>
            ) : (
              <form onSubmit={submitLink} className="mt-3 flex flex-col gap-2 text-xs">
                <div className="flex flex-wrap items-center gap-3">
                  <label className="flex items-center gap-1.5">
                    <input type="radio" name={`link-mode-${app.id}`} checked={!publicLink} onChange={() => setPublicLink(false)} />
                    With a password
                  </label>
                  <label className="flex items-center gap-1.5">
                    <input type="radio" name={`link-mode-${app.id}`} checked={publicLink} onChange={() => setPublicLink(true)} />
                    Public <span className="rounded-sm bg-status-warning-bg px-1 py-px text-3xs text-status-warning-fg">anyone with the URL</span>
                  </label>
                  <select value={linkExpiry} onChange={(e) => setLinkExpiry(e.target.value)} aria-label="Link expiry"
                    className="ml-auto rounded-control border border-strong bg-surface-card px-2 py-1 text-xs text-secondary focus:border-action focus:outline-none">
                    {linkExpiryChoices.map((c) => <option key={c.value} value={c.value}>{c.label}</option>)}
                  </select>
                </div>
                {publicLink && (
                  <label className="flex items-start gap-1.5 text-2xs text-status-warning-fg">
                    <input type="checkbox" checked={publicUnderstood} onChange={(e) => setPublicUnderstood(e.target.checked)} />
                    I understand anyone holding this URL can open the app without a password.
                  </label>
                )}
                {hasSteps && (
                  <p className="text-2xs text-status-warning-fg" data-testid="share-steps-note">
                    This app runs scripts on its own. Whatever a visitor of the link sends is data to those scripts, never instructions.
                  </p>
                )}
                {platformActions.length > 0 && (
                  <label className="flex items-start gap-1.5 text-2xs text-secondary">
                    <input type="checkbox" checked={linkButtons} onChange={(e) => setLinkButtons(e.target.checked)} aria-label="Buttons on the link" />
                    <span>
                      Let the link use the buttons {platformActions.map((a) => `“${a.label}”`).join(', ')}
                      {app.scope === PERSONAL_SCOPE
                        ? '. They run with your connected accounts, as you.'
                        : '. They run on the agent\'s own authority.'}
                    </span>
                  </label>
                )}
                <div className="flex items-center gap-2">
                  <Button
                    variant="primary"
                    size="xs"
                    type="submit"
                    disabled={create.isPending || !settingsKnown || (publicLink && !publicUnderstood)}
                  >
                    Make link
                  </Button>
                  {notice
                    ? <span className="text-2xs text-status-success-fg" role="status">{notice}</span>
                    : <span className="text-3xs text-tertiary">You will be asked to confirm it is you.</span>}
                </div>
              </form>
            )}
          </>
        )}

        {confirmAsk && (
          <form
            onSubmit={(e) => { e.preventDefault(); void confirmAsk.retry({ password: confirmPassword }) }}
            className="mt-3 rounded-card bg-surface-sunken p-3 text-xs"
          >
            {confirmAsk.method === 'oidc' ? (
              <>
                <p className="font-medium text-primary">Confirm it is you</p>
                <p className="mt-1 text-secondary">
                  This page goes to {confirmAsk.provider} to check it is you, then comes back here.
                  {isChat ? ' Open the share again when you are back.' : ' The link and its password are shown when you return.'}
                </p>
                <div className="mt-2 flex gap-2">
                  <Button variant="primary" size="xs" type="button" onClick={continueToProvider}>
                    Continue to {confirmAsk.provider}
                  </Button>
                  <Button size="xs" type="button" onClick={() => setConfirmAsk(null)}>
                    Cancel
                  </Button>
                </div>
              </>
            ) : confirmAsk.method === 'password' ? (
              <>
                <p className="font-medium text-primary">Confirm it is you</p>
                <div className="mt-2 flex gap-2">
                  <input type="password" autoComplete="current-password" value={confirmPassword}
                    onChange={(e) => setConfirmPassword(e.target.value)} aria-label="Your password" placeholder="Your password"
                    className="min-w-0 flex-1 rounded-control border border-strong bg-surface-card px-2 py-1.5 text-sm text-primary focus:border-action focus:outline-none" />
                  <Button variant="primary" size="xs" type="submit" disabled={!confirmPassword}>
                    Confirm
                  </Button>
                  <Button
                    size="xs"
                    type="button"
                    onClick={() => { setConfirmAsk(null); setConfirmPassword('') }}
                  >
                    Cancel
                  </Button>
                </div>
              </>
            ) : (
              <p className="text-secondary">
                Links need you to confirm it is you. This account has no password or passkey to do that.
              </p>
            )}
          </form>
        )}
        {/* Everything this is shared with, people, agents, departments and
            links alike, under either tab: one glance says who can open it. */}
        <div className="mt-3 border-t border-subtle pt-2" data-testid="share-list">
          <p className="text-3xs font-semibold uppercase tracking-wide text-tertiary">Shared with</p>
          {isLoading ? (
            <p className="py-1.5 text-xs text-tertiary">Loading…</p>
          ) : loadError ? (
            <p className="py-1.5 text-xs text-status-danger-fg">{(loadError as Error).message}</p>
          ) : internal.length || external.length ? (
            <ul className="divide-y divide-subtle">
              {internal.map(shareRow)}
              {external.map(linkRow)}
            </ul>
          ) : (
            <p className="py-1.5 text-xs text-tertiary">Nobody yet.</p>
          )}
        </div>
        {error && <p className="mt-2 text-2xs text-status-danger-fg" role="alert">{error}</p>}
    </Dialog>
  )
}
