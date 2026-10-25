import { useState, useRef, useEffect, useLayoutEffect } from 'react'
import { flushSync } from 'react-dom'
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import { fetchAuthConfig, signOutEverywhere } from '../../api/auth'
import { useAuth } from '../../contexts/AuthContext'
import { ROLE, allowedOnSharedOnly, roleBadge, roleLabel } from '../../lib/permissions'
import { isSharedOnly, modeOfAgent } from '../../lib/visibility'
import { useSetPlatformAuth } from '../../api/executionLayers'
import { apiJson, apiOk, withMessage } from '../../lib/http/apiJson'
import { SaveState } from '../../components/ui/SettingsControls'
import { useSaveState, type SaveStateApi } from '../../hooks/useSaveState'
import Switch from '../../components/ui/Switch'
import { blockedByModal } from '../../lib/ui/modalLayers'
import { useDialogs } from '../../components/ui/dialogs'
import { revealOnAttach } from '../../components/common/useInvokedForm'
import Button from '../../components/ui/Button'
import { Input, Select } from '../../components/ui/Field'
import { BADGE_BASE, BADGE_TONE, CARD, POPOVER } from '../../components/ui/classes'
import { PAGE_TITLE } from '../../components/ui/classes'
import InfoTip from '../../components/ui/InfoTip'

interface UserRecord {
  sub: string
  email: string
  name: string
  role: string
  agents: string[]
  agent_roles: Record<string, string>
  default_agent: string
  allow_platform_auth: number
  created_at: string
  last_login: string
  auth_provider?: string
  is_owner?: boolean
  local_only?: number
  invite_pending?: boolean
}

function useUsers() {
  return useQuery({
    queryKey: ['admin-users'],
    queryFn: async () => {
      const data = await apiJson<{ users: UserRecord[] }>('/v1/admin/users').catch(withMessage('Failed to fetch users'))
      return data.users
    },
  })
}

function useAgentList() {
  return useQuery({
    queryKey: ['admin-agent-names'],
    queryFn: async () => {
      const data = await apiJson<{ agents: { name: string; collaborative?: boolean; default_scope?: 'user' | 'agent' }[] }>(
        '/v1/agents?all=true',
      ).catch(withMessage('Failed to fetch agents'))
      // A Shared-only agent takes editor or manager assignments only.
      return data.agents
        .map((a) => ({ name: a.name, sharedOnly: isSharedOnly(modeOfAgent(a)) }))
    },
  })
}

function formatDate(iso: string): string {
  if (!iso) return '\u2014'
  const d = new Date(iso)
  return d.toLocaleDateString(undefined, { month: 'short', day: 'numeric', year: 'numeric' })
}

const SELECT_CLS = 'text-sm border border-strong rounded-control px-2 py-1 bg-surface-card text-primary'
const SELECT_XS_CLS = 'text-3xs border rounded-control px-1 py-0.5 bg-surface-card text-primary'

/**
 * Agents-and-roles popover, triggered by a compact "N agents" chip next to the
 * user's name. Keeps the full agent list off the resting card (which clutters
 * fast) while staying one click away. Click-to-toggle so it works on touch.
 */
function AgentsPopover({ user }: { user: UserRecord }) {
  const [open, setOpen] = useState(false)
  const [shiftX, setShiftX] = useState(0)
  const ref = useRef<HTMLDivElement>(null)
  const popRef = useRef<HTMLDivElement>(null)

  useEffect(() => {
    if (!open) return
    const onDown = (e: MouseEvent) => {
      if (ref.current && !ref.current.contains(e.target as Node)) setOpen(false)
    }
    const onEsc = (e: KeyboardEvent) => { if (e.key === 'Escape' && !blockedByModal(e)) setOpen(false) }
    document.addEventListener('mousedown', onDown)
    document.addEventListener('keydown', onEsc)
    return () => {
      document.removeEventListener('mousedown', onDown)
      document.removeEventListener('keydown', onEsc)
    }
  }, [open])

  // Keep the popover on-screen: it's anchored to the trigger's left edge, so on
  // a narrow viewport it can run past the right edge — shift it left by exactly
  // the overflow. `max-w-[80vw]` guarantees the shift never clips the left edge.
  useLayoutEffect(() => {
    if (!open || !ref.current || !popRef.current) { setShiftX(0); return }
    const margin = 8
    const left = ref.current.getBoundingClientRect().left
    const overflow = left + popRef.current.offsetWidth - (window.innerWidth - margin)
    setShiftX(overflow > 0 ? -overflow : 0)
  }, [open])

  if (user.agents.length === 0) {
    return <span className="text-3xs text-tertiary">No agents</span>
  }

  return (
    <div ref={ref} className="relative">
      <button
        type="button"
        onClick={() => setOpen((o) => !o)}
        title="View agents and roles"
        aria-expanded={open}
        className={`inline-flex items-center gap-1 px-1.5 py-0.5 rounded-control text-3xs font-medium border transition-colors ${
          open ? 'border-transparent bg-action-subtle text-link' : 'border-transparent bg-surface-muted text-secondary bg-linear-to-b from-transparent to-transparent hover:from-surface-hover hover:to-surface-hover'
        }`}
      >
        <svg className="w-3 h-3" fill="none" stroke="currentColor" viewBox="0 0 24 24">
          <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2}
            d="M17 20h5v-2a4 4 0 00-3-3.87M9 20H4v-2a4 4 0 013-3.87m6-2.13a4 4 0 10-4-4 4 4 0 004 4zm6 0a4 4 0 00-1-7.75" />
        </svg>
        {user.agents.length} {user.agents.length === 1 ? 'agent' : 'agents'}
      </button>
      {open && (
        <div ref={popRef} style={{ transform: shiftX ? `translateX(${shiftX}px)` : undefined }}
          className={`${POPOVER} absolute z-20 left-0 mt-1 w-60 max-w-[80vw] p-2`}>
          <p className="text-3xs uppercase tracking-wide text-tertiary px-1 pb-1.5">Agents and roles</p>
          <div className="flex flex-wrap gap-1.5">
            {user.agents.map((a) => {
              const tag = { label: roleLabel(user.agent_roles?.[a]), cls: roleBadge(user.agent_roles?.[a]) }
              const isDefault = a === user.default_agent
              return (
                <span key={a}
                  className={`inline-flex items-center gap-1 pl-1.5 pr-1 py-0.5 rounded-control border text-xs ${
                    isDefault ? 'border-transparent bg-action-subtle' : 'border-subtle'
                  }`}>
                  {isDefault && <span title="Default agent" className="text-status-warning-fg leading-none">&#9733;</span>}
                  <span className="text-primary">{a}</span>
                  <span className={`px-1 py-px rounded-sm text-3xs font-medium leading-none ${tag.cls}`}>{tag.label}</span>
                </span>
              )
            })}
          </div>
        </div>
      )}
    </div>
  )
}

// The two switches save on their own; the form shows their save state
// beside each label.
function PlatformAuthToggle({ user, run }: { user: UserRecord; run: SaveStateApi['run'] }) {
  const setPlatformAuth = useSetPlatformAuth()
  const checked = !!user.allow_platform_auth
  return (
    <Switch
      aria-label="Platform API"
      checked={checked}
      title={checked ? 'May borrow admin API credentials, but not OAuth subscriptions' : 'Own subscription only'}
      onChange={(allowed) => { void run(`${user.sub}:platform_auth`, () => setPlatformAuth.mutateAsync({ userSub: user.sub, allowed }), { failTitle: 'The Platform API access was not changed' }) }}
      disabled={setPlatformAuth.isPending}
    />
  )
}

function LocalOnlyToggle({ user, run }: { user: UserRecord; run: SaveStateApi['run'] }) {
  const queryClient = useQueryClient()
  const toggle = useMutation({
    mutationFn: async ({ sub, localOnly }: { sub: string; localOnly: boolean }) => {
      await apiOk(`/v1/admin/users/${sub}/local-only`, {
        method: 'PUT',
        body: JSON.stringify({ local_only: localOnly }),
      }).catch(withMessage('The server did not accept the change.'))
    },
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ['admin-users'] }),
  })
  const checked = !!user.local_only
  return (
    <Switch
      aria-label="LAN only"
      checked={checked}
      title={checked ? 'LAN only: sign-in only from the local network' : 'No network restriction'}
      onChange={(localOnly) => { void run(`${user.sub}:local_only`, () => toggle.mutateAsync({ sub: user.sub, localOnly }), { failTitle: 'The LAN-only setting was not saved' }) }}
      disabled={toggle.isPending}
    />
  )
}

export default function UsersPage() {
  const dialogs = useDialogs()
  const { data: users, isLoading } = useUsers()
  const { data: allAgents } = useAgentList()
  const { user: authUser, refreshUser } = useAuth()
  const queryClient = useQueryClient()
  const [editingSub, setEditingSub] = useState<string | null>(null)
  const [editRole, setEditRole] = useState('')
  const [editAgents, setEditAgents] = useState<string[]>([])
  const [editAgentRoles, setEditAgentRoles] = useState<Record<string, string>>({})
  const [error, setError] = useState<string | null>(null)
  const { stateOf, run } = useSaveState()
  const [search, setSearch] = useState('')

  // Add User modal
  const [showAddUser, setShowAddUser] = useState(false)
  const [newEmail, setNewEmail] = useState('')
  const [newDisplayName, setNewDisplayName] = useState('')
  const [newRole, setNewRole] = useState('member')
  const [newMode, setNewMode] = useState<'invite' | 'password'>('invite')
  const [newPassword, setNewPassword] = useState('')
  const [sendEmailInvite, setSendEmailInvite] = useState(false)
  const [tempPasswordResult, setTempPasswordResult] = useState('')
  const [inviteResult, setInviteResult] = useState<{ url: string; sent: boolean } | null>(null)

  // Only needed for the "email the invite" checkbox — emailing a link needs
  // SMTP AND a public dashboard URL (the copy-out link works without either).
  const { data: authConfig } = useQuery({ queryKey: ['auth-config'], queryFn: fetchAuthConfig })
  const canEmailInvites = !!authConfig?.email_links_available

  // Reset password result
  const [resetPasswordResult, setResetPasswordResult] = useState<{ sub: string; password: string } | null>(null)
  const resetButtonRef = useRef<HTMLButtonElement>(null)

  const updateRole = useMutation({
    mutationFn: async ({ sub, role }: { sub: string; role: string }) => {
      await apiOk(`/v1/admin/users/${sub}/role`, {
        method: 'PUT',
        body: JSON.stringify({ role }),
      }, 'Failed to update role')
    },
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ['admin-users'] }),
    onError: (e: Error) => setError(e.message),
  })

  const updateAgents = useMutation({
    mutationFn: async ({ sub, agents, agent_roles }: { sub: string; agents: string[]; agent_roles?: Record<string, string> }) => {
      await apiOk(`/v1/admin/users/${sub}/agents`, {
        method: 'PUT',
        body: JSON.stringify({ agents, agent_roles }),
      }, 'Failed to update agents')
    },
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ['admin-users'] }),
    onError: (e: Error) => setError(e.message),
  })

  const deleteUser = useMutation({
    mutationFn: async (sub: string) => {
      await apiOk(`/v1/admin/users/${sub}/delete`, { method: 'POST' }, 'Failed to delete user')
    },
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ['admin-users'] }),
    onError: (e: Error) => setError(e.message),
  })

  const createUser = useMutation({
    mutationFn: async (data: { email: string; display_name: string; role: string; password?: string; send_invite?: boolean }) => {
      return apiJson<{ temp_password?: string; invite_url?: string; invite_sent?: boolean }>('/v1/admin/users', {
        method: 'POST',
        body: JSON.stringify(data),
      }, 'Failed to create user')
    },
    onSuccess: (data) => {
      queryClient.invalidateQueries({ queryKey: ['admin-users'] })
      if (data.temp_password) {
        setTempPasswordResult(data.temp_password)
      } else if (data.invite_url) {
        // Relative when the install has no public URL configured — resolve
        // against this dashboard's own origin for the copy-out.
        setInviteResult({
          url: new URL(data.invite_url, window.location.origin).toString(),
          sent: !!data.invite_sent,
        })
      } else {
        setShowAddUser(false)
      }
    },
    onError: (e: Error) => setError(e.message),
  })

  const signOutUserEverywhere = useMutation({
    mutationFn: (sub: string) => signOutEverywhere(sub),
    onError: (e: Error) => setError(e.message),
  })

  const resetUserPassword = useMutation({
    mutationFn: async (sub: string) => {
      return apiJson<{ temp_password: string }>(`/v1/admin/users/${sub}/reset-password`, { method: 'POST' }, 'Failed to reset password')
    },
    onSuccess: (data, sub) => {
      setResetPasswordResult({ sub, password: data.temp_password })
    },
    onError: (e: Error) => setError(e.message),
  })

  const handleCreateUser = () => {
    createUser.mutate({
      email: newEmail, display_name: newDisplayName, role: newRole,
      password: newMode === 'password' ? newPassword : undefined,
      send_invite: newMode === 'invite' ? sendEmailInvite : undefined,
    })
  }

  const startEdit = (u: UserRecord) => {
    setEditingSub(u.sub)
    setEditRole(u.role)
    setEditAgents([...u.agents])
    setEditAgentRoles({ ...u.agent_roles })
    setError(null)
  }

  const saveEdit = async () => {
    if (!editingSub) return
    const current = users?.find((u) => u.sub === editingSub)
    if (!current) return

    if (editRole !== current.role) {
      await updateRole.mutateAsync({ sub: editingSub, role: editRole })
    }
    const agentsChanged =
      editAgents.length !== current.agents.length ||
      editAgents.some((a) => !current.agents.includes(a))
    const rolesChanged = editAgents.some(
      (a) => (editAgentRoles[a] || startingRole(a)) !== (current.agent_roles?.[a] || ROLE.VIEWER)
    )
    if (agentsChanged || rolesChanged) {
      // Every ticked agent carries its role explicitly (a Shared-only agent
      // starts at editor, never the store's viewer default).
      const agentRoles = Object.fromEntries(editAgents.map((a) => [a, editAgentRoles[a] || startingRole(a)]))
      await updateAgents.mutateAsync({ sub: editingSub, agents: editAgents, agent_roles: agentRoles })
      // Editing YOUR OWN assignments must also refresh the auth snapshot —
      // `user.agent_roles` is otherwise only fetched at app load, and it
      // drives the Remote Machines settings tab and per-agent role gates.
      if (editingSub === authUser?.sub) void refreshUser()
    }
    setEditingSub(null)
  }

  const sharedOnly = new Set((allAgents || []).filter((a) => a.sharedOnly).map((a) => a.name))
  // The role a newly ticked agent starts with: viewer, or editor on a
  // Shared-only agent (its chats run as the agent, which takes editor).
  const startingRole = (agent: string) => (sharedOnly.has(agent) ? ROLE.EDITOR : ROLE.VIEWER)

  const toggleAgent = (agent: string) => {
    if (!editAgents.includes(agent) && !editAgentRoles[agent] && sharedOnly.has(agent)) {
      setEditAgentRoles((prev) => ({ ...prev, [agent]: ROLE.EDITOR }))
    }
    setEditAgents((prev) =>
      prev.includes(agent) ? prev.filter((a) => a !== agent) : [...prev, agent],
    )
  }

  const isSaving = updateRole.isPending || updateAgents.isPending

  // --- Agent edit checkboxes with role dropdowns ---
  const renderAgentEditor = () => (
    <div className="space-y-2">
      <div className="flex flex-col gap-1.5">
        {allAgents?.map(({ name: agent, sharedOnly: agentSharedOnly }) => {
          const role = editAgentRoles[agent] || startingRole(agent)
          // A Shared-only agent offers editor and manager only; a lower row an
          // older install holds shows, marked, and cannot be picked again.
          const legacy = agentSharedOnly && !allowedOnSharedOnly(role)
          return (
            <label key={agent} className="flex items-center gap-2 min-w-0 text-xs text-primary">
              <input
                type="checkbox"
                checked={editAgents.includes(agent)}
                onChange={() => toggleAgent(agent)}
                className="accent-action shrink-0"
              />
              <span className="min-w-[120px]">
                {agent}
                {agentSharedOnly && <span className="ml-1 text-3xs text-tertiary">Shared only</span>}
              </span>
              {/* The role select shrinks to what the row has left (its
                  longest option is wider than a phone) and clips its closed
                  text; the native picker still shows every option in full. */}
              {editAgents.includes(agent) && (
                <select
                  value={role}
                  onChange={(e) => setEditAgentRoles((prev) => ({ ...prev, [agent]: e.target.value }))}
                  aria-label={`Role on ${agent}`}
                  className={`${SELECT_XS_CLS} min-w-0 truncate ${legacy ? 'border-status-warning-solid' : 'border-strong'}`}
                  title={legacy ? 'Shared only takes the editor role or above: this assignment opens no chat. Raise it or untick the agent.' : undefined}
                >
                  {(!agentSharedOnly || role === ROLE.VIEWER) && (
                    <option value={ROLE.VIEWER} disabled={agentSharedOnly}>
                      {agentSharedOnly ? 'Viewer: needs editor on Shared only' : 'Viewer: reads and chats'}
                    </option>
                  )}
                  {(!agentSharedOnly || role === ROLE.CONTRIBUTOR) && (
                    <option value={ROLE.CONTRIBUTOR} disabled={agentSharedOnly}>
                      {agentSharedOnly ? 'Contributor: needs editor on Shared only' : 'Contributor: also writes the shared workspace'}
                    </option>
                  )}
                  <option value={ROLE.EDITOR}>Editor: also automates as the agent</option>
                  <option value={ROLE.MANAGER}>Manager: configures the agent</option>
                </select>
              )}
            </label>
          )
        })}
      </div>
    </div>
  )

  // --- Shared edit form (desktop + mobile) ---
  // The one-time password, where the Reset was pressed: in the card of the
  // user it was reset for, scrolled into view (on a desktop Copy takes the
  // focus), never at the top of the page above a long list.
  // Dismiss takes its own button away: the focus goes back to Reset
  // Password, where the reset was asked for, when the edit form is open.
  const dismissResetResult = () => {
    flushSync(() => setResetPasswordResult(null))
    if (resetButtonRef.current?.isConnected) resetButtonRef.current.focus()
  }
  const resetResultPanel = (password: string) => (
    <div ref={revealOnAttach} data-testid="reset-password-result">
      <div className="rounded-card border border-status-warning-border bg-status-warning-bg p-4">
        <p className="text-sm font-medium text-status-warning-fg mb-2">Password Reset</p>
        <p className="text-xs text-status-warning-fg mb-2">New temporary password. Copy it now. It won't be shown again.</p>
        <div className="flex gap-2 mb-3">
          <code className="flex-1 px-3 py-2 text-sm font-mono bg-surface-card border border-subtle rounded-control text-primary select-all">{password}</code>
          <Button size="sm" onClick={() => navigator.clipboard.writeText(password)}>Copy</Button>
        </div>
        <Button variant="primary" size="xs" onClick={dismissResetResult}>Dismiss</Button>
      </div>
    </div>
  )

  const renderEditForm = (u: UserRecord) => {
    const isLocal = (u.auth_provider || 'local').startsWith('local')
    return (
      <div className="space-y-4">
        {/* Role + access — two columns on desktop, stacked on mobile */}
        <div className="grid grid-cols-1 sm:grid-cols-2 gap-4">
          <div>
            <label className="text-xs text-secondary block mb-1">Role</label>
            <select value={editRole} onChange={(e) => setEditRole(e.target.value)} className={`${SELECT_CLS} w-full sm:w-44`}>
              <option value="admin">Admin</option>
              <option value="creator">Creator</option>
              <option value="member">Member</option>
            </select>
          </div>
          <div>
            <label className="text-xs text-secondary block mb-1">Access</label>
            <div className="space-y-1.5">
              <div className="flex items-center justify-between gap-3 max-w-[15rem]">
                <div className="min-w-0">
                  <span className="text-xs text-secondary">Platform API</span>
                  <InfoTip label="Platform API">Lets this user borrow admin API keys. This applies when they have no subscription of their own. Admin OAuth subscriptions are never lent.</InfoTip>
                  <SaveState state={stateOf(`${u.sub}:platform_auth`)} />
                </div>
                <PlatformAuthToggle user={u} run={run} />
              </div>
              {isLocal && (
                <div className="flex items-center justify-between gap-3 max-w-[15rem]">
                  <div className="min-w-0">
                    <span className="text-xs text-secondary">LAN only</span>
                    <InfoTip label="LAN only">Restrict this account to local network only</InfoTip>
                    <SaveState state={stateOf(`${u.sub}:local_only`)} />
                  </div>
                  <LocalOnlyToggle user={u} run={run} />
                </div>
              )}
            </div>
          </div>
        </div>
        <div>
          <div className="mb-1">
            <label className="text-xs text-secondary">Agents</label>
            <InfoTip label="Agents">
              Roles on each agent are separate from the platform role. The platform role
              only covers platform actions, like creating agents and admin settings.
            </InfoTip>
          </div>
          {renderAgentEditor()}
        </div>

        {/* Account actions — kept out of the resting card to declutter it.
            Proper buttons so the admin sees them clearly. */}
        {(isLocal || !Number(u.is_owner)) && (
          <div className="pt-3 border-t border-subtle space-y-1.5">
            <label className="text-xs text-secondary block">Account actions</label>
            <div className="flex flex-wrap gap-2">
              {isLocal && (
                <Button variant="secondary" size="xs" ref={resetButtonRef} onClick={async () => {
                    if (await dialogs.confirm({ title: `Reset the password of ${u.email}?`, confirmLabel: 'Reset password' })) resetUserPassword.mutate(u.sub)
                  }}>
                  Reset Password
                </Button>
              )}
              {!Number(u.is_owner) && (
                <Button variant="secondary" size="xs" onClick={async () => {
                    if (await dialogs.confirm({
                      title: `Sign ${u.email} out of every device?`,
                      body: 'Their open chats restart at their next message.',
                      confirmLabel: 'Sign out everywhere',
                      tone: 'danger',
                    })) signOutUserEverywhere.mutate(u.sub)
                  }}>
                  Sign Out Everywhere
                </Button>
              )}
              {!Number(u.is_owner) && (
                <Button variant="danger" size="xs" onClick={async () => {
                    if (await dialogs.confirm({ title: `Delete the user ${u.email}?`, confirmLabel: 'Delete', tone: 'danger' })) deleteUser.mutate(u.sub)
                  }}>
                  Delete User
                </Button>
              )}
            </div>
          </div>
        )}
        {resetPasswordResult?.sub === u.sub && resetResultPanel(resetPasswordResult.password)}

        <div className="flex gap-2 pt-1">
          <Button variant="primary" size="xs" onClick={saveEdit} disabled={isSaving}>
            {isSaving ? 'Saving…' : 'Save'}
          </Button>
          <Button variant="secondary" size="xs" onClick={() => setEditingSub(null)}>
            Cancel
          </Button>
        </div>
      </div>
    )
  }

  // Rendered only for SSO/OIDC users — local users show no badge ("no badge =
  // local"). "sso" is an acronym → uppercase (was "Sso"); others title-case.
  const AuthBadge = ({ user: u }: { user: UserRecord }) => {
    const name = (u.auth_provider || '').replace('oidc:', '')
    const label = name.toLowerCase() === 'sso' ? 'SSO' : name.charAt(0).toUpperCase() + name.slice(1)
    return (
      <span className={`${BADGE_BASE} ${BADGE_TONE.info}`}>
        {label}
      </span>
    )
  }

  if (isLoading) return <p className="text-sm text-secondary">Loading users...</p>

  const q = search.trim().toLowerCase()
  const filteredUsers = (users || []).filter(
    (u) => !q || (u.name || '').toLowerCase().includes(q) || (u.email || '').toLowerCase().includes(q),
  )

  const openAddUser = () => {
    setShowAddUser(true); setNewEmail(''); setNewDisplayName(''); setNewRole('member')
    setNewMode('invite'); setNewPassword(''); setSendEmailInvite(false)
    setTempPasswordResult(''); setInviteResult(null); setError(null)
  }

  return (
    <div className="space-y-4">
      {/* Desktop: title left, search and Add User right. Phone: Add User on
          row 1, the search full width on row 2. */}
      <div className="flex flex-wrap items-center justify-end gap-3">
        <h2 className={`${PAGE_TITLE} mr-auto`}>Users</h2>
        <div className="relative order-last w-full sm:order-none sm:w-60">
          <svg className="absolute left-2.5 top-1/2 -translate-y-1/2 w-4 h-4 text-tertiary pointer-events-none"
            fill="none" stroke="currentColor" viewBox="0 0 24 24">
            <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M21 21l-4.35-4.35M11 18a7 7 0 100-14 7 7 0 000 14z" />
          </svg>
          <Input
            type="search"
            value={search}
            onChange={(e) => setSearch(e.target.value)}
            placeholder="Search users…"
            className="pl-8"
          />
        </div>
        <Button variant="primary" size="sm" onClick={openAddUser}
          className="shrink-0">
          Add User
        </Button>
      </div>

      {/* Add User Modal */}
      {showAddUser && (
        <div className={`${CARD} p-4 space-y-3`}>
          <h3 className="text-sm font-semibold text-primary">Create New User</h3>
          {tempPasswordResult ? (
            <div className="space-y-3">
              <p className="text-sm text-status-success-fg">User created successfully.</p>
              <div>
                <label className="block text-xs text-secondary mb-1">Temporary password. Copy it now. It won't be shown again.</label>
                <div className="flex gap-2">
                  <code className="flex-1 px-3 py-2 text-sm font-mono bg-surface-sunken rounded-control text-primary select-all">{tempPasswordResult}</code>
                  <Button variant="secondary" size="xs" onClick={() => navigator.clipboard.writeText(tempPasswordResult)}>Copy</Button>
                </div>
              </div>
              <Button variant="primary" size="xs" onClick={() => { setShowAddUser(false); setTempPasswordResult('') }}>Done</Button>
            </div>
          ) : inviteResult ? (
            <div className="space-y-3">
              <p className="text-sm text-status-success-fg">
                User created successfully.{inviteResult.sent && ' Invite email sent.'}
              </p>
              <div>
                <label className="block text-xs text-secondary mb-1">Invite link. Copy it now: it won't be shown again. It expires in 48 hours.</label>
                <div className="flex gap-2">
                  <code className="flex-1 px-3 py-2 text-sm font-mono bg-surface-sunken rounded-control text-primary select-all break-all">{inviteResult.url}</code>
                  <Button variant="secondary" size="xs" onClick={() => navigator.clipboard.writeText(inviteResult.url)}>Copy</Button>
                </div>
              </div>
              <Button variant="primary" size="xs" onClick={() => { setShowAddUser(false); setInviteResult(null) }}>Done</Button>
            </div>
          ) : (
            <>
              <div className="grid grid-cols-1 sm:grid-cols-2 gap-3">
                <div>
                  <label className="block text-xs text-secondary mb-1">Display Name</label>
                  <Input type="text" value={newDisplayName} onChange={e => setNewDisplayName(e.target.value)}
                    placeholder="John Smith" />
                </div>
                <div>
                  <label className="block text-xs text-secondary mb-1">Email</label>
                  <Input type="email" value={newEmail} onChange={e => setNewEmail(e.target.value)}
                    placeholder="user@example.com" />
                </div>
                <div>
                  <label className="block text-xs text-secondary mb-1">Role</label>
                  <Select value={newRole} onChange={e => setNewRole(e.target.value)}>
                    <option value="admin">Admin</option>
                    <option value="creator">Creator</option>
                    <option value="member">Member</option>
                  </Select>
                </div>
                <div>
                  <label className="block text-xs text-secondary mb-1">Account Setup</label>
                  <div className="space-y-1.5 pt-1">
                    <label className="flex items-center gap-2 text-xs text-primary">
                      <input type="radio" name="account-setup" checked={newMode === 'invite'}
                        onChange={() => setNewMode('invite')} className="shrink-0" />
                      Invite link: the user sets their own password
                    </label>
                    <label className="flex items-center gap-2 text-xs text-primary">
                      <input type="radio" name="account-setup" checked={newMode === 'password'}
                        onChange={() => setNewMode('password')} className="shrink-0" />
                      Temporary password
                    </label>
                  </div>
                </div>
                {newMode === 'password' ? (
                  <div>
                    <label className="block text-xs text-secondary mb-1">Temporary Password</label>
                    <Input type="text" value={newPassword} onChange={e => setNewPassword(e.target.value)}
                      placeholder="Set a temporary password" />
                    <p className="text-3xs text-tertiary mt-0.5">User will be required to change it on first login</p>
                  </div>
                ) : (
                  <div className="self-end">
                    <label className={`flex items-center gap-2 text-xs ${canEmailInvites ? 'text-primary' : 'text-tertiary'}`}>
                      <input type="checkbox" checked={sendEmailInvite && canEmailInvites}
                        disabled={!canEmailInvites}
                        onChange={e => setSendEmailInvite(e.target.checked)}
                        className="accent-action shrink-0" />
                      Also email the invite link
                    </label>
                    {!canEmailInvites && (
                      <p className="text-3xs text-tertiary mt-0.5">Needs a public dashboard URL and SMTP in Setup, Security</p>
                    )}
                  </div>
                )}
              </div>
              <div className="flex gap-2 pt-1">
                <Button variant="primary" size="xs" onClick={handleCreateUser}
                  disabled={createUser.isPending || !newEmail || !newDisplayName || (newMode === 'password' && !newPassword)}>
                  {createUser.isPending ? 'Creating...' : 'Create User'}
                </Button>
                <Button variant="secondary" size="xs" onClick={() => setShowAddUser(false)}>Cancel</Button>
              </div>
            </>
          )}
        </div>
      )}

      {error && (
        <div className="bg-status-danger-bg border border-status-danger-border rounded-card p-3 text-sm text-status-danger-fg">
          {error}
          <button onClick={() => setError(null)} className="ml-2 underline">dismiss</button>
        </div>
      )}

      {/* User list — unified card layout (desktop + mobile) */}
      {(!users || users.length === 0) ? (
        <p className={`${CARD} text-sm text-tertiary py-4 text-center`}>
          No users yet. Add a user to get started.
        </p>
      ) : filteredUsers.length === 0 ? (
        <p className={`${CARD} text-sm text-tertiary py-4 text-center`}>
          No users match “{search.trim()}”.
        </p>
      ) : null}
      <div className="space-y-3">
        {filteredUsers.map((u) => {
          const isEditing = editingSub === u.sub
          const isLocal = (u.auth_provider || 'local').startsWith('local')
          return (
            <div key={u.sub} className={CARD}>
              {/* Header row */}
              <div className="px-4 py-3 flex items-center justify-between gap-3">
                <div className="flex items-center gap-3 min-w-0">
                  {/* Avatar circle */}
                  <div className="w-9 h-9 rounded-full bg-surface-muted flex items-center justify-center shrink-0">
                    <span className="text-sm font-semibold text-secondary">
                      {(u.name || u.email)[0]?.toUpperCase()}
                    </span>
                  </div>
                  <div className="min-w-0">
                    <div className="flex items-center gap-1.5 flex-wrap">
                      <p className="font-medium text-sm text-primary truncate">{u.name}</p>
                      {!!u.is_owner && <span title="Platform owner" className="text-status-warning-fg text-sm">&#9733;</span>}
                      <span className={`${BADGE_BASE} ${roleBadge(u.role)}`}>{u.role}</span>
                      {/* Only SSO/OIDC users get an auth badge — "no badge = local". */}
                      {!isLocal && <AuthBadge user={u} />}
                      <AgentsPopover user={u} />
                    </div>
                    <p className="text-xs text-secondary truncate">{u.email}</p>
                  </div>
                </div>
                <div className="flex items-center gap-2 shrink-0">
                  {!isEditing && (
                    <button onClick={() => startEdit(u)} className="text-xs text-link hover:text-link-hover font-medium">Edit</button>
                  )}
                </div>
              </div>

              {/* Resting, the one-time password shows under the card's
                  header; with the edit form open, under its Account actions. */}
              {!isEditing && resetPasswordResult?.sub === u.sub && (
                <div className="px-4 pb-3">{resetResultPanel(resetPasswordResult.password)}</div>
              )}

              {/* Expanded content */}
              {isEditing ? (
                <div className="px-4 pb-4 border-t border-subtle pt-3">
                  {renderEditForm(u)}
                </div>
              ) : (
                /* Resting body — just status chips + dates. Agents live in the
                   header popover; account actions live in the Edit form. */
                <div className="px-4 pb-3 flex flex-wrap items-center gap-x-4 gap-y-1 text-xs text-tertiary">
                  {!!u.allow_platform_auth && (
                    <span className={`${BADGE_BASE} ${BADGE_TONE.info}`}
                      title="May borrow admin API credentials when they have no subscription of their own">Platform API</span>
                  )}
                  {isLocal && !!u.local_only && (
                    <span className={`${BADGE_BASE} ${BADGE_TONE.warning}`}
                      title="Login restricted to the local network">LAN only</span>
                  )}
                  {!!u.invite_pending && (
                    <span className={`${BADGE_BASE} ${BADGE_TONE.info}`}
                      title="No password set yet. The invite link has not been used yet. Reset the password to give them a new way in.">Invite pending</span>
                  )}
                  <span>Last login {formatDate(u.last_login)}</span>
                  <span>Created {formatDate(u.created_at)}</span>
                </div>
              )}
            </div>
          )
        })}
      </div>
    </div>
  )
}
