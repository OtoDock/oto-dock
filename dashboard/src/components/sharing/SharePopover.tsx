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
import { pushEscHandler } from '../../lib/escStack'
import { formatExpiry } from '../../lib/format'
import { AGENT_ROLES, ROLE, actingRole, isAdmin, rank, roleLabel, type AgentRole } from '../../lib/permissions'
import { savePending, takeConfirm, takePending, type PendingShare } from '../../lib/shareConfirm'

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

const EXPIRY_CHOICES: Array<{ value: string; label: string }> = [
  { value: '', label: 'Never expires' },
  { value: '7d', label: 'Expires in 7 days' },
  { value: '30d', label: 'Expires in 30 days' },
  { value: '90d', label: 'Expires in 90 days' },
]

// An empty expiry on a link becomes the server's 30 days, so a link that
// must not expire (a printed QR code) says "never", which an admin cap
// refuses: the choice is offered only once the settings say nothing caps it.
const LINK_EXPIRY_CHOICES = EXPIRY_CHOICES.filter((c) => c.value)
const LINK_NEVER_CHOICE = { value: 'never', label: 'Never expires' }

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
  const { data: sharingSettings } = useSharingSettings()
  const linkExpiryChoices = sharingSettings && sharingSettings.max_expiry_days === null
    ? [...LINK_EXPIRY_CHOICES, LINK_NEVER_CHOICE]
    : LINK_EXPIRY_CHOICES
  const [includeTools, setIncludeTools] = useState(false)
  const create = useCreateShare()
  const patch = usePatchShare()
  const user = useAuth()?.user ?? null
  // The account the identity-provider round trip files its entries under.
  const sub = user?.sub ?? ''
  const [tab, setTab] = useState<Tab>(initialTab ?? 'people')
  const [who, setWho] = useState('')
  const [kind, setKind] = useState<GranteeKind>(GRANTEE_KIND.PERSON)
  const [agentPick, setAgentPick] = useState('')
  const [deptPick, setDeptPick] = useState('')
  const [cap, setCap] = useState<AgentRole>(ROLE.VIEWER)
  const [expiresIn, setExpiresIn] = useState('')
  const [notice, setNotice] = useState('')
  const [error, setError] = useState('')
  const [copied, setCopied] = useState('')
  // Link form.
  const [publicLink, setPublicLink] = useState(false)
  const [publicUnderstood, setPublicUnderstood] = useState(false)
  const [linkExpiry, setLinkExpiry] = useState('30d')
  const [linkButtons, setLinkButtons] = useState(false)
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
  const [armed, setArmed] = useState<{ token: string; op: 'create' | 'patch'; share_id?: string } | null>(null)

  useEffect(() => pushEscHandler(onClose), [onClose])

  // Back from the identity provider (SHARING.md "The confirm"): the
  // choices come back as they were, the link tab opens, and the token arms
  // the button. Nothing fires without a click.
  useEffect(() => {
    const pending = takePending(sub, target.id)
    if (!pending) return
    setTab('link')
    setPublicLink(pending.fields.public)
    setPublicUnderstood(pending.fields.public)
    setLinkExpiry(pending.fields.expiry)
    setLinkButtons(pending.fields.buttons)
    setIncludeTools(pending.fields.include_tools)
    if (confirmError) {
      setError(confirmError)
      return
    }
    const token = takeConfirm(sub)
    if (!token) {
      setError('Your confirmation took too long. Make the link again.')
      return
    }
    setArmed({ token, op: pending.op, share_id: pending.share_id })
    setNotice(pending.op === 'create' ? 'Confirmed. Make the link now.' : 'Confirmed. Turn the buttons on now.')
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

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
  const canSubmit = !!grantee && !create.isPending

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
          <p className="truncate text-sm text-p-text">
            {prefix}{name}
            {capWord && <span className="ml-1 rounded bg-p-accent-teal/15 px-1 py-px text-[10px] text-p-accent-teal">{capWord}</span>}
          </p>
          {state && <p className="text-[11px] text-p-text-light">{state}</p>}
        </div>
        {s.state === 'suspended' && (
          <button type="button" onClick={() => patch.mutate({ id: s.id, resume: true }, { onError: failed })}
            className="rounded-md border border-p-border-light px-2 py-0.5 text-[11px] text-p-text-secondary hover:bg-p-surface-hover">
            Resume
          </button>
        )}
        <button type="button" onClick={() => patch.mutate({ id: s.id, revoke: true }, { onError: failed })}
          className="rounded-md px-2 py-0.5 text-[11px] text-red-600 hover:bg-red-500/10 dark:text-red-400"
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
          <p className="truncate text-sm text-p-text">
            Link
            {s.public && <span className="ml-1 rounded bg-amber-500/15 px-1 py-px text-[10px] text-amber-700 dark:text-amber-300">public</span>}
            {s.state === 'suspended' && <span className="ml-1 text-[10px] text-amber-600">paused</span>}
          </p>
          <p className="text-[11px] text-p-text-light">{bits.join(' · ')}</p>
        </div>
        {platformActions.length > 0 && (
          <label className="flex items-center gap-1 text-[11px] text-p-text-secondary" title="Platform buttons on this link">
            <input type="checkbox" checked={s.allow_actions} onChange={() => toggleButtons(s)} aria-label={`Buttons on link ${s.id}`} />
            Buttons
          </label>
        )}
        <button type="button" onClick={() => patch.mutate({ id: s.id, revoke: true }, { onError: failed })}
          className="rounded-md px-2 py-0.5 text-[11px] text-red-600 hover:bg-red-500/10 dark:text-red-400"
          aria-label={`Revoke link ${s.id}`}>
          Revoke
        </button>
      </li>
    )
  }

  const tabBtn = (t: Tab, label: string) => (
    <button type="button" onClick={() => { setTab(t); setError(''); setNotice('') }}
      className={`flex-1 rounded-md px-2 py-1 text-center font-medium ${tab === t ? 'bg-brand-surface text-brand' : 'text-p-text-light hover:text-p-text'}`}>
      {label}
    </button>
  )

  const subtitle = isChat
    ? 'A read-only copy of the conversation as it stands now. Share again to send a newer one.'
    : app.scope === 'shared'
      ? 'Members of this agent already see it. Add a person, place it in another agent, or make a link.'
      : 'Only you see this app. Add a colleague, or make a link.'

  const kindLabel = (k: GranteeKind) => k === GRANTEE_KIND.PERSON ? 'A person' : k === GRANTEE_KIND.AGENT ? 'An agent' : 'A department'
  const selectClass = 'min-w-0 flex-1 rounded-md border border-p-border-light bg-p-bg px-2 py-1.5 text-sm pointer-coarse:text-base text-p-text focus:border-brand focus:outline-none'

  return (
    <div className="fixed inset-0 z-[60] flex items-end justify-center bg-black/40 p-3 sm:items-center" onClick={onClose} role="presentation">
      <div role="dialog" aria-modal="true" aria-label={`Share ${title}`} onClick={(e) => e.stopPropagation()}
        className="max-h-[90dvh] w-full max-w-md overflow-y-auto rounded-2xl border border-p-border-light bg-p-surface p-4 text-p-text shadow-xl">
        <div className="flex items-start gap-2">
          <div className="min-w-0 flex-1">
            <h2 className="truncate text-sm font-semibold">Share “{title}”</h2>
            <p className="text-[11px] text-p-text-light">{subtitle}</p>
          </div>
          <button type="button" onClick={onClose} aria-label="Close"
            className="flex h-7 w-7 items-center justify-center rounded-full text-p-text-light hover:bg-p-surface-hover hover:text-p-text">
            <svg className="h-4 w-4" fill="none" stroke="currentColor" viewBox="0 0 24 24" strokeWidth={2}>
              <path strokeLinecap="round" strokeLinejoin="round" d="M6 18L18 6M6 6l12 12" />
            </svg>
          </button>
        </div>

        <div className="mt-3 flex items-center gap-1 rounded-lg border border-p-border-light p-0.5 text-xs">
          {tabBtn('people', 'People')}
          {tabBtn('link', 'Link')}
        </div>
        {isChat && (
          <label className="mt-2 flex items-center gap-1.5 text-[11px] text-p-text-secondary">
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
                      className="min-w-0 flex-1 rounded-md border border-p-border-light bg-p-bg px-2 py-1.5 text-sm pointer-coarse:text-base text-p-text placeholder:text-p-text-light focus:border-brand focus:outline-none" />
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
                  className="rounded-md border border-p-border-light bg-p-bg px-2 py-1.5 text-xs pointer-coarse:text-base text-p-text-secondary focus:border-brand focus:outline-none">
                  {EXPIRY_CHOICES.map((c) => <option key={c.value || 'never'} value={c.value}>{c.label}</option>)}
                </select>
              </div>
              {capChoices.length > 0 && (
                <label className="flex items-center gap-2 text-[11px] text-p-text-secondary">
                  <span className="shrink-0">They act as</span>
                  <select value={capChoices.includes(cap) ? cap : ROLE.VIEWER} onChange={(e) => setCap(e.target.value as AgentRole)} aria-label="Role"
                    className="min-w-0 flex-1 rounded-md border border-p-border-light bg-p-bg px-2 py-1 text-xs pointer-coarse:text-base text-p-text focus:border-brand focus:outline-none">
                    {capChoices.map((r) => <option key={r} value={r}>{roleLabel(r)}</option>)}
                  </select>
                </label>
              )}
              <div className="flex items-center gap-2">
                <button type="submit" disabled={!canSubmit}
                  className="rounded-md bg-brand px-3 py-1.5 text-xs font-medium text-white transition-colors hover:bg-brand-hover disabled:opacity-50">
                  Share
                </button>
                {notice && <span className="text-[11px] text-p-text-secondary">{notice}</span>}
              </div>
            </form>
            {!isChat && (
              <>
                <div className="mt-3 flex items-center gap-2 border-t border-p-border-light/60 pt-2">
                  <code className="min-w-0 flex-1 truncate rounded bg-black/5 px-2 py-1 font-mono text-[11px] text-p-text-secondary dark:bg-white/5" title={link}>{link}</code>
                  <button type="button" onClick={() => copy(link, 'link')}
                    className="rounded-md border border-p-border-light px-2 py-1 text-[11px] text-p-text-secondary hover:bg-p-surface-hover">
                    {copied === 'link' ? 'Copied' : 'Copy link'}
                  </button>
                </div>
                <p className="mt-1 text-[10px] text-p-text-light">The link opens for members and for everyone above after they sign in.</p>
              </>
            )}
          </>
        )}

        {tab === 'link' && (
          <>
            {made ? (
              <div className="mt-3 rounded-lg border border-emerald-500/40 bg-emerald-500/5 p-3 text-xs">
                <p className="font-medium text-p-text">Your link is ready. The password is shown only now.</p>
                <div className="mt-2 flex items-center gap-2">
                  <code className="min-w-0 flex-1 truncate rounded bg-black/5 px-2 py-1 font-mono text-[11px] dark:bg-white/5" title={made.link}>{made.link}</code>
                  <button type="button" onClick={() => copy(made.link || '', 'made-link')}
                    className="rounded-md border border-p-border-light px-2 py-1 text-[11px] text-p-text-secondary hover:bg-p-surface-hover">
                    {copied === 'made-link' ? 'Copied' : 'Copy link'}
                  </button>
                </div>
                {made.password && (
                  <div className="mt-2 flex items-center gap-2">
                    <span className="text-p-text-light">Password</span>
                    <code className="rounded bg-black/5 px-2 py-1 font-mono text-[12px] tracking-wider dark:bg-white/5">{made.password}</code>
                    <button type="button" onClick={() => copy(made.password || '', 'password')}
                      className="rounded-md border border-p-border-light px-2 py-1 text-[11px] text-p-text-secondary hover:bg-p-surface-hover">
                      {copied === 'password' ? 'Copied' : 'Copy'}
                    </button>
                  </div>
                )}
                <button type="button" onClick={() => setMade(null)} className="mt-2 text-[11px] text-p-text-light underline decoration-dotted">
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
                    Public <span className="rounded bg-amber-500/15 px-1 py-px text-[10px] text-amber-700 dark:text-amber-300">anyone with the URL</span>
                  </label>
                  <select value={linkExpiry} onChange={(e) => setLinkExpiry(e.target.value)} aria-label="Link expiry"
                    className="ml-auto rounded-md border border-p-border-light bg-p-bg px-2 py-1 text-xs pointer-coarse:text-base text-p-text-secondary focus:border-brand focus:outline-none">
                    {linkExpiryChoices.map((c) => <option key={c.value} value={c.value}>{c.label}</option>)}
                  </select>
                </div>
                {publicLink && (
                  <label className="flex items-start gap-1.5 text-[11px] text-amber-700 dark:text-amber-300">
                    <input type="checkbox" checked={publicUnderstood} onChange={(e) => setPublicUnderstood(e.target.checked)} />
                    I understand anyone holding this URL can open the app without a password.
                  </label>
                )}
                {hasSteps && (
                  <p className="text-[11px] text-amber-700 dark:text-amber-300" data-testid="share-steps-note">
                    This app runs scripts on its own. Whatever a visitor of the link sends is data to those scripts, never instructions.
                  </p>
                )}
                {platformActions.length > 0 && (
                  <label className="flex items-start gap-1.5 text-[11px] text-p-text-secondary">
                    <input type="checkbox" checked={linkButtons} onChange={(e) => setLinkButtons(e.target.checked)} aria-label="Buttons on the link" />
                    <span>
                      Let the link use the buttons {platformActions.map((a) => `“${a.label}”`).join(', ')}
                      {app.scope === PERSONAL_SCOPE
                        ? ' — they run with your connected accounts, as you.'
                        : ' — they run on the agent\'s own authority.'}
                    </span>
                  </label>
                )}
                <div className="flex items-center gap-2">
                  <button type="submit" disabled={create.isPending || (publicLink && !publicUnderstood)}
                    className="rounded-md bg-brand px-3 py-1.5 text-xs font-medium text-white transition-colors hover:bg-brand-hover disabled:opacity-50">
                    Make link
                  </button>
                  {notice
                    ? <span className="text-[11px] text-emerald-700 dark:text-emerald-300" role="status">{notice}</span>
                    : <span className="text-[10px] text-p-text-light">You will be asked to confirm it is you.</span>}
                </div>
              </form>
            )}
          </>
        )}

        {confirmAsk && (
          <form
            onSubmit={(e) => { e.preventDefault(); void confirmAsk.retry({ password: confirmPassword }) }}
            className="mt-3 rounded-lg border border-p-border-light bg-p-bg p-3 text-xs"
          >
            {confirmAsk.method === 'oidc' ? (
              <>
                <p className="font-medium text-p-text">Confirm it is you</p>
                <p className="mt-1 text-p-text-secondary">
                  This page goes to {confirmAsk.provider} to check it is you, then comes back here.
                  {isChat ? ' Open the share again when you are back.' : ' The link and its password are shown when you return.'}
                </p>
                <div className="mt-2 flex gap-2">
                  <button type="button" onClick={continueToProvider}
                    className="rounded-md bg-brand px-3 py-1.5 text-xs font-medium text-white hover:bg-brand-hover">
                    Continue to {confirmAsk.provider}
                  </button>
                  <button type="button" onClick={() => setConfirmAsk(null)}
                    className="rounded-md border border-p-border-light px-2 py-1.5 text-xs text-p-text-secondary hover:bg-p-surface-hover">
                    Cancel
                  </button>
                </div>
              </>
            ) : confirmAsk.method === 'password' ? (
              <>
                <p className="font-medium text-p-text">Confirm it is you</p>
                <div className="mt-2 flex gap-2">
                  <input type="password" autoComplete="current-password" value={confirmPassword}
                    onChange={(e) => setConfirmPassword(e.target.value)} aria-label="Your password" placeholder="Your password"
                    className="min-w-0 flex-1 rounded-md border border-p-border-light bg-p-surface px-2 py-1.5 text-sm pointer-coarse:text-base text-p-text focus:border-brand focus:outline-none" />
                  <button type="submit" disabled={!confirmPassword}
                    className="rounded-md bg-brand px-3 py-1.5 text-xs font-medium text-white hover:bg-brand-hover disabled:opacity-50">
                    Confirm
                  </button>
                  <button type="button" onClick={() => { setConfirmAsk(null); setConfirmPassword('') }}
                    className="rounded-md border border-p-border-light px-2 py-1.5 text-xs text-p-text-secondary hover:bg-p-surface-hover">
                    Cancel
                  </button>
                </div>
              </>
            ) : (
              <p className="text-p-text-secondary">
                Links need a confirmation, and this account has neither a password nor a passkey to confirm with.
              </p>
            )}
          </form>
        )}
        {/* Everything this is shared with, people, agents, departments and
            links alike, under either tab: one glance says who can open it. */}
        <div className="mt-3 border-t border-p-border-light/60 pt-2" data-testid="share-list">
          <p className="text-[10px] font-semibold uppercase tracking-wide text-p-text-light">Shared with</p>
          {isLoading ? (
            <p className="py-1.5 text-xs text-p-text-light">Loading…</p>
          ) : loadError ? (
            <p className="py-1.5 text-xs text-red-600 dark:text-red-400">{(loadError as Error).message}</p>
          ) : internal.length || external.length ? (
            <ul className="divide-y divide-p-border-light/60">
              {internal.map(shareRow)}
              {external.map(linkRow)}
            </ul>
          ) : (
            <p className="py-1.5 text-xs text-p-text-light">Nobody yet.</p>
          )}
        </div>
        {error && <p className="mt-2 text-[11px] text-red-600 dark:text-red-400" role="alert">{error}</p>}
      </div>
    </div>
  )
}
