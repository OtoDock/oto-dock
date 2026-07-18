import { useState, useRef, useEffect, useLayoutEffect } from 'react'
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import { apiFetch, fetchAuthConfig } from '../../api/auth'
import { useAuth } from '../../contexts/AuthContext'
import { useSetPlatformAuth } from '../../api/executionLayers'

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
      const res = await apiFetch('/v1/admin/users')
      if (!res.ok) throw new Error('Failed to fetch users')
      const data = await res.json()
      return data.users as UserRecord[]
    },
  })
}

function useAgentList() {
  return useQuery({
    queryKey: ['admin-agent-names'],
    queryFn: async () => {
      const res = await apiFetch('/v1/agents?all=true')
      if (!res.ok) throw new Error('Failed to fetch agents')
      const data = await res.json()
      return (data.agents as { name: string }[]).map((a) => a.name)
    },
  })
}

function formatDate(iso: string): string {
  if (!iso) return '\u2014'
  const d = new Date(iso)
  return d.toLocaleDateString(undefined, { month: 'short', day: 'numeric', year: 'numeric' })
}

const ROLE_BADGE: Record<string, string> = {
  admin: 'bg-red-100 dark:bg-red-900/30 text-red-700 dark:text-red-400',
  creator: 'bg-brand-100 text-brand',
  member: 'bg-p-surface text-p-text-secondary',
}

const SELECT_CLS = 'text-sm border border-p-border-light rounded-sm px-2 py-1 bg-white dark:bg-p-surface text-p-text'
const SELECT_XS_CLS = 'text-[10px] border border-p-border-light rounded-sm px-1 py-0.5 bg-white dark:bg-p-surface text-p-text'

// Per-agent role tag shown on every agent chip. Viewer was previously rendered
// with no indicator at all (looked role-less); now all three are explicit.
const AGENT_ROLE_TAG: Record<string, { label: string; cls: string }> = {
  manager: { label: 'Manager', cls: 'bg-brand-100 text-brand' },
  editor: { label: 'Editor', cls: 'bg-amber-100 text-amber-700 dark:bg-amber-900/30 dark:text-amber-400' },
  viewer: { label: 'Viewer', cls: 'bg-gray-100 dark:bg-gray-800 text-p-text-secondary' },
}

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
    const onEsc = (e: KeyboardEvent) => { if (e.key === 'Escape') setOpen(false) }
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
    return <span className="text-[10px] text-p-text-light">No agents</span>
  }

  return (
    <div ref={ref} className="relative">
      <button
        type="button"
        onClick={() => setOpen((o) => !o)}
        title="View agents & roles"
        aria-expanded={open}
        className={`inline-flex items-center gap-1 px-1.5 py-0.5 rounded-sm text-[10px] font-medium border transition-colors ${
          open ? 'border-brand/40 bg-brand/5 text-brand' : 'border-p-border-light bg-p-surface text-p-text-secondary hover:bg-p-surface-hover'
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
          className="absolute z-20 left-0 mt-1 w-60 max-w-[80vw] p-2 rounded-lg border border-p-border-light bg-white dark:bg-p-surface shadow-lg">
          <p className="text-[10px] uppercase tracking-wide text-p-text-light px-1 pb-1.5">Agents &amp; roles</p>
          <div className="flex flex-wrap gap-1.5">
            {user.agents.map((a) => {
              const tag = AGENT_ROLE_TAG[user.agent_roles?.[a] || 'viewer'] || AGENT_ROLE_TAG.viewer
              const isDefault = a === user.default_agent
              return (
                <span key={a}
                  className={`inline-flex items-center gap-1 pl-1.5 pr-1 py-0.5 rounded-md border text-xs ${
                    isDefault ? 'border-brand/40 bg-brand/5' : 'border-p-border-light bg-white dark:bg-p-surface'
                  }`}>
                  {isDefault && <span title="Default agent" className="text-amber-500 leading-none">&#9733;</span>}
                  <span className="text-p-text">{a}</span>
                  <span className={`px-1 py-px rounded-sm text-[10px] font-medium leading-none ${tag.cls}`}>{tag.label}</span>
                </span>
              )
            })}
          </div>
        </div>
      )}
    </div>
  )
}

function PlatformAuthToggle({ user }: { user: UserRecord }) {
  const setPlatformAuth = useSetPlatformAuth()
  const checked = !!user.allow_platform_auth
  return (
    <button
      type="button"
      role="switch"
      aria-checked={checked}
      title={checked ? 'May borrow admin API credentials (not OAuth subscriptions)' : 'Own subscription only'}
      onClick={() => setPlatformAuth.mutate({ userSub: user.sub, allowed: !checked })}
      disabled={setPlatformAuth.isPending}
      className={`relative inline-flex h-5 w-9 shrink-0 cursor-pointer rounded-full border-2 border-transparent transition-colors duration-200 focus:outline-hidden disabled:opacity-50
        ${checked ? 'bg-brand' : 'bg-gray-300 dark:bg-gray-600'}`}
    >
      <span
        className={`pointer-events-none inline-block h-4 w-4 transform rounded-full bg-white shadow-sm ring-0 transition duration-200
          ${checked ? 'translate-x-4' : 'translate-x-0'}`}
      />
    </button>
  )
}

function LocalOnlyToggle({ user }: { user: UserRecord }) {
  const queryClient = useQueryClient()
  const toggle = useMutation({
    mutationFn: async ({ sub, localOnly }: { sub: string; localOnly: boolean }) => {
      const res = await apiFetch(`/v1/admin/users/${sub}/local-only`, {
        method: 'PUT',
        body: JSON.stringify({ local_only: localOnly }),
      })
      if (!res.ok) throw new Error('Failed')
    },
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ['admin-users'] }),
  })
  const checked = !!user.local_only
  return (
    <button type="button" role="switch" aria-checked={checked}
      title={checked ? 'LAN only — login restricted to local network' : 'No network restriction'}
      onClick={() => toggle.mutate({ sub: user.sub, localOnly: !checked })}
      disabled={toggle.isPending}
      className={`relative inline-flex h-5 w-9 shrink-0 cursor-pointer rounded-full border-2 border-transparent transition-colors duration-200 focus:outline-hidden disabled:opacity-50
        ${checked ? 'bg-brand' : 'bg-gray-300 dark:bg-gray-600'}`}>
      <span className={`pointer-events-none inline-block h-4 w-4 transform rounded-full bg-white shadow-sm ring-0 transition duration-200
        ${checked ? 'translate-x-4' : 'translate-x-0'}`} />
    </button>
  )
}

export default function UsersPage() {
  const { data: users, isLoading } = useUsers()
  const { data: allAgents } = useAgentList()
  const { user: authUser, refreshUser } = useAuth()
  const queryClient = useQueryClient()
  const [editingSub, setEditingSub] = useState<string | null>(null)
  const [editRole, setEditRole] = useState('')
  const [editAgents, setEditAgents] = useState<string[]>([])
  const [editAgentRoles, setEditAgentRoles] = useState<Record<string, string>>({})
  const [error, setError] = useState<string | null>(null)
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

  const updateRole = useMutation({
    mutationFn: async ({ sub, role }: { sub: string; role: string }) => {
      const res = await apiFetch(`/v1/admin/users/${sub}/role`, {
        method: 'PUT',
        body: JSON.stringify({ role }),
      })
      if (!res.ok) {
        const d = await res.json().catch(() => ({}))
        throw new Error(d.detail || 'Failed to update role')
      }
    },
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ['admin-users'] }),
    onError: (e: Error) => setError(e.message),
  })

  const updateAgents = useMutation({
    mutationFn: async ({ sub, agents, agent_roles }: { sub: string; agents: string[]; agent_roles?: Record<string, string> }) => {
      const res = await apiFetch(`/v1/admin/users/${sub}/agents`, {
        method: 'PUT',
        body: JSON.stringify({ agents, agent_roles }),
      })
      if (!res.ok) {
        const d = await res.json().catch(() => ({}))
        throw new Error(d.detail || 'Failed to update agents')
      }
    },
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ['admin-users'] }),
    onError: (e: Error) => setError(e.message),
  })

  const deleteUser = useMutation({
    mutationFn: async (sub: string) => {
      const res = await apiFetch(`/v1/admin/users/${sub}/delete`, { method: 'POST' })
      if (!res.ok) {
        const d = await res.json().catch(() => ({}))
        throw new Error(d.detail || 'Failed to delete user')
      }
    },
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ['admin-users'] }),
    onError: (e: Error) => setError(e.message),
  })

  const createUser = useMutation({
    mutationFn: async (data: { email: string; display_name: string; role: string; password?: string; send_invite?: boolean }) => {
      const res = await apiFetch('/v1/admin/users', {
        method: 'POST',
        body: JSON.stringify(data),
      })
      if (!res.ok) {
        const d = await res.json().catch(() => ({}))
        throw new Error(d.detail || 'Failed to create user')
      }
      return res.json()
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

  const resetUserPassword = useMutation({
    mutationFn: async (sub: string) => {
      const res = await apiFetch(`/v1/admin/users/${sub}/reset-password`, { method: 'POST' })
      if (!res.ok) {
        const d = await res.json().catch(() => ({}))
        throw new Error(d.detail || 'Failed to reset password')
      }
      return res.json()
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
      (a) => (editAgentRoles[a] || 'viewer') !== (current.agent_roles?.[a] || 'viewer')
    )
    if (agentsChanged || rolesChanged) {
      await updateAgents.mutateAsync({ sub: editingSub, agents: editAgents, agent_roles: editAgentRoles })
      // Editing YOUR OWN assignments must also refresh the auth snapshot —
      // `user.agent_roles` is otherwise only fetched at app load, and it
      // drives the Remote Machines settings tab and per-agent role gates.
      if (editingSub === authUser?.sub) void refreshUser()
    }
    setEditingSub(null)
  }

  const toggleAgent = (agent: string) => {
    setEditAgents((prev) =>
      prev.includes(agent) ? prev.filter((a) => a !== agent) : [...prev, agent],
    )
  }

  const isSaving = updateRole.isPending || updateAgents.isPending

  // --- Agent edit checkboxes with role dropdowns ---
  const renderAgentEditor = () => (
    <div className="space-y-2">
      <div className="flex flex-col gap-1.5">
        {allAgents?.map((agent) => (
          <label key={agent} className="flex items-center gap-2 text-xs text-p-text">
            <input
              type="checkbox"
              checked={editAgents.includes(agent)}
              onChange={() => toggleAgent(agent)}
              className="rounded-sm border-p-border-light shrink-0"
            />
            <span className="min-w-[120px]">{agent}</span>
            {editAgents.includes(agent) && editRole !== 'member' && (
              <select
                value={editAgentRoles[agent] || 'viewer'}
                onChange={(e) => setEditAgentRoles((prev) => ({ ...prev, [agent]: e.target.value }))}
                className={SELECT_XS_CLS}
              >
                <option value="viewer">viewer</option>
                <option value="editor">editor</option>
                <option value="manager">manager</option>
              </select>
            )}
          </label>
        ))}
      </div>
    </div>
  )

  // --- Shared edit form (desktop + mobile) ---
  const renderEditForm = (u: UserRecord) => {
    const isLocal = (u.auth_provider || 'local').startsWith('local')
    return (
      <div className="space-y-4">
        {/* Role + access — two columns on desktop, stacked on mobile */}
        <div className="grid grid-cols-1 sm:grid-cols-2 gap-4">
          <div>
            <label className="text-xs text-p-text-secondary block mb-1">Role</label>
            <select value={editRole} onChange={(e) => setEditRole(e.target.value)} className={`${SELECT_CLS} w-full sm:w-44`}>
              <option value="admin">Admin</option>
              <option value="creator">Creator</option>
              <option value="member">Member</option>
            </select>
          </div>
          <div>
            <label className="text-xs text-p-text-secondary block mb-1">Access</label>
            <div className="space-y-1.5">
              <div className="flex items-center justify-between gap-3 max-w-[15rem]">
                <span className="text-xs text-p-text-secondary" title="Allow this user to borrow admin API credentials (API keys) when they have no subscription of their own — never admin OAuth subscriptions">Platform Auth</span>
                <PlatformAuthToggle user={u} />
              </div>
              {isLocal && (
                <div className="flex items-center justify-between gap-3 max-w-[15rem]">
                  <span className="text-xs text-p-text-secondary" title="Restrict this account to local network only">LAN only</span>
                  <LocalOnlyToggle user={u} />
                </div>
              )}
            </div>
          </div>
        </div>
        <div>
          <label className="text-xs text-p-text-secondary block mb-1">Agents</label>
          {renderAgentEditor()}
        </div>

        {/* Account actions — kept out of the resting card to declutter it.
            Proper bordered buttons so the admin sees them clearly. */}
        {(isLocal || !Number(u.is_owner)) && (
          <div className="pt-3 border-t border-p-border-light space-y-1.5">
            <label className="text-xs text-p-text-secondary block">Account actions</label>
            <div className="flex flex-wrap gap-2">
              {isLocal && (
                <button onClick={() => { if (window.confirm(`Reset password for ${u.email}?`)) resetUserPassword.mutate(u.sub) }}
                  className="text-xs px-3 py-1.5 rounded-sm border border-amber-300 dark:border-amber-700 text-amber-700 dark:text-amber-400 hover:bg-amber-50 dark:hover:bg-amber-900/20 transition-colors">
                  Reset Password
                </button>
              )}
              {!Number(u.is_owner) && (
                <button onClick={() => { if (window.confirm(`Delete user ${u.email}?`)) deleteUser.mutate(u.sub) }}
                  className="text-xs px-3 py-1.5 rounded-sm border border-red-300 dark:border-red-800 text-red-600 dark:text-red-400 hover:bg-red-50 dark:hover:bg-red-900/20 transition-colors">
                  Delete User
                </button>
              )}
            </div>
          </div>
        )}

        <div className="flex gap-2 pt-1">
          <button onClick={saveEdit} disabled={isSaving}
            className="text-xs px-3 py-1.5 bg-brand text-white rounded-sm hover:bg-brand-hover disabled:opacity-50">
            {isSaving ? 'Saving…' : 'Save'}
          </button>
          <button onClick={() => setEditingSub(null)}
            className="text-xs px-3 py-1.5 border border-p-border-light rounded-sm text-p-text-secondary hover:bg-p-surface-hover">
            Cancel
          </button>
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
      <span className="text-[10px] px-1.5 py-0.5 rounded-sm font-medium bg-purple-50 dark:bg-purple-900/20 text-purple-600 dark:text-purple-400">
        {label}
      </span>
    )
  }

  if (isLoading) return <p className="text-sm text-p-text-secondary">Loading users...</p>

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
      {/* Header — desktop: title left, [search][Add User] right.
          Mobile: title + Add User on row 1, search full-width on row 2. */}
      <div className="flex flex-wrap items-center gap-3">
        <h2 className="text-lg font-bold text-p-text mr-auto">Users</h2>
        <div className="relative order-last w-full sm:order-none sm:w-60">
          <svg className="absolute left-2.5 top-1/2 -translate-y-1/2 w-4 h-4 text-p-text-light pointer-events-none"
            fill="none" stroke="currentColor" viewBox="0 0 24 24">
            <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M21 21l-4.35-4.35M11 18a7 7 0 100-14 7 7 0 000 14z" />
          </svg>
          <input
            type="search"
            value={search}
            onChange={(e) => setSearch(e.target.value)}
            placeholder="Search users…"
            className="w-full pl-8 pr-3 py-1.5 text-sm border border-p-border-light rounded-lg bg-white dark:bg-p-surface text-p-text focus:outline-hidden focus:ring-2 focus:ring-brand/30"
          />
        </div>
        <button onClick={openAddUser}
          className="shrink-0 px-3 py-1.5 text-sm font-medium text-white bg-brand hover:bg-brand-hover rounded-lg transition-colors">
          Add User
        </button>
      </div>

      {/* Add User Modal */}
      {showAddUser && (
        <div className="border border-p-border-light rounded-xl bg-white dark:bg-p-surface p-4 space-y-3">
          <h3 className="text-sm font-semibold text-p-text">Create New User</h3>
          {tempPasswordResult ? (
            <div className="space-y-3">
              <p className="text-sm text-green-600 dark:text-green-400">User created successfully.</p>
              <div>
                <label className="block text-xs text-p-text-secondary mb-1">Temporary Password (copy now — won't be shown again)</label>
                <div className="flex gap-2">
                  <code className="flex-1 px-3 py-2 text-sm font-mono bg-p-bg border border-p-border-light rounded-lg text-p-text select-all">{tempPasswordResult}</code>
                  <button onClick={() => navigator.clipboard.writeText(tempPasswordResult)}
                    className="px-3 py-2 text-xs border border-p-border-light rounded-lg text-p-text-secondary hover:bg-p-surface-hover">Copy</button>
                </div>
              </div>
              <button onClick={() => { setShowAddUser(false); setTempPasswordResult('') }}
                className="text-xs px-3 py-1.5 bg-brand text-white rounded-sm hover:bg-brand-hover">Done</button>
            </div>
          ) : inviteResult ? (
            <div className="space-y-3">
              <p className="text-sm text-green-600 dark:text-green-400">
                User created successfully.{inviteResult.sent && ' Invite email sent.'}
              </p>
              <div>
                <label className="block text-xs text-p-text-secondary mb-1">Invite link (copy now — won't be shown again; expires in 48 hours)</label>
                <div className="flex gap-2">
                  <code className="flex-1 px-3 py-2 text-sm font-mono bg-p-bg border border-p-border-light rounded-lg text-p-text select-all break-all">{inviteResult.url}</code>
                  <button onClick={() => navigator.clipboard.writeText(inviteResult.url)}
                    className="px-3 py-2 text-xs border border-p-border-light rounded-lg text-p-text-secondary hover:bg-p-surface-hover">Copy</button>
                </div>
              </div>
              <button onClick={() => { setShowAddUser(false); setInviteResult(null) }}
                className="text-xs px-3 py-1.5 bg-brand text-white rounded-sm hover:bg-brand-hover">Done</button>
            </div>
          ) : (
            <>
              <div className="grid grid-cols-1 sm:grid-cols-2 gap-3">
                <div>
                  <label className="block text-xs text-p-text-secondary mb-1">Display Name</label>
                  <input type="text" value={newDisplayName} onChange={e => setNewDisplayName(e.target.value)}
                    placeholder="John Smith"
                    className="w-full px-3 py-2 text-sm border border-p-border-light rounded-lg bg-white dark:bg-p-surface text-p-text focus:outline-hidden focus:ring-2 focus:ring-brand/30" />
                </div>
                <div>
                  <label className="block text-xs text-p-text-secondary mb-1">Email</label>
                  <input type="email" value={newEmail} onChange={e => setNewEmail(e.target.value)}
                    placeholder="user@example.com"
                    className="w-full px-3 py-2 text-sm border border-p-border-light rounded-lg bg-white dark:bg-p-surface text-p-text focus:outline-hidden focus:ring-2 focus:ring-brand/30" />
                </div>
                <div>
                  <label className="block text-xs text-p-text-secondary mb-1">Role</label>
                  <select value={newRole} onChange={e => setNewRole(e.target.value)}
                    className="w-full px-3 py-2 text-sm border border-p-border-light rounded-lg bg-white dark:bg-p-surface text-p-text">
                    <option value="admin">Admin</option>
                    <option value="creator">Creator</option>
                    <option value="member">Member</option>
                  </select>
                </div>
                <div>
                  <label className="block text-xs text-p-text-secondary mb-1">Account Setup</label>
                  <div className="space-y-1.5 pt-1">
                    <label className="flex items-center gap-2 text-xs text-p-text">
                      <input type="radio" name="account-setup" checked={newMode === 'invite'}
                        onChange={() => setNewMode('invite')} className="shrink-0" />
                      Invite link — user sets their own password
                    </label>
                    <label className="flex items-center gap-2 text-xs text-p-text">
                      <input type="radio" name="account-setup" checked={newMode === 'password'}
                        onChange={() => setNewMode('password')} className="shrink-0" />
                      Temporary password
                    </label>
                  </div>
                </div>
                {newMode === 'password' ? (
                  <div>
                    <label className="block text-xs text-p-text-secondary mb-1">Temporary Password</label>
                    <input type="text" value={newPassword} onChange={e => setNewPassword(e.target.value)}
                      placeholder="Set a temporary password"
                      className="w-full px-3 py-2 text-sm border border-p-border-light rounded-lg bg-white dark:bg-p-surface text-p-text focus:outline-hidden focus:ring-2 focus:ring-brand/30" />
                    <p className="text-[10px] text-p-text-light mt-0.5">User will be required to change it on first login</p>
                  </div>
                ) : (
                  <div className="self-end">
                    <label className={`flex items-center gap-2 text-xs ${canEmailInvites ? 'text-p-text' : 'text-p-text-light'}`}>
                      <input type="checkbox" checked={sendEmailInvite && canEmailInvites}
                        disabled={!canEmailInvites}
                        onChange={e => setSendEmailInvite(e.target.checked)}
                        className="rounded-sm border-p-border-light shrink-0" />
                      Also email the invite link
                    </label>
                    {!canEmailInvites && (
                      <p className="text-[10px] text-p-text-light mt-0.5">Needs SMTP (Platform → Security) and a public dashboard URL</p>
                    )}
                  </div>
                )}
              </div>
              <div className="flex gap-2 pt-1">
                <button onClick={handleCreateUser}
                  disabled={createUser.isPending || !newEmail || !newDisplayName || (newMode === 'password' && !newPassword)}
                  className="text-xs px-3 py-1.5 bg-brand text-white rounded-sm hover:bg-brand-hover disabled:opacity-50">
                  {createUser.isPending ? 'Creating...' : 'Create User'}
                </button>
                <button onClick={() => setShowAddUser(false)}
                  className="text-xs px-3 py-1.5 border border-p-border-light rounded-sm text-p-text-secondary hover:bg-p-surface-hover">Cancel</button>
              </div>
            </>
          )}
        </div>
      )}

      {/* Reset Password Result */}
      {resetPasswordResult && (
        <div className="border border-amber-200 dark:border-amber-800 rounded-xl bg-amber-50 dark:bg-amber-900/20 p-4">
          <p className="text-sm font-medium text-amber-800 dark:text-amber-300 mb-2">Password Reset</p>
          <p className="text-xs text-amber-700 dark:text-amber-400 mb-2">New temporary password (copy now — won't be shown again):</p>
          <div className="flex gap-2 mb-3">
            <code className="flex-1 px-3 py-2 text-sm font-mono bg-white dark:bg-p-surface border border-p-border-light rounded-lg text-p-text select-all">{resetPasswordResult.password}</code>
            <button onClick={() => navigator.clipboard.writeText(resetPasswordResult.password)}
              className="px-3 py-2 text-xs border border-p-border-light rounded-lg text-p-text-secondary hover:bg-p-surface-hover">Copy</button>
          </div>
          <button onClick={() => setResetPasswordResult(null)}
            className="text-xs px-3 py-1.5 bg-brand text-white rounded-sm hover:bg-brand-hover">Dismiss</button>
        </div>
      )}

      {error && (
        <div className="bg-red-50 dark:bg-red-900/20 border border-red-200 dark:border-red-800 rounded-lg p-3 text-sm text-red-700 dark:text-red-400">
          {error}
          <button onClick={() => setError(null)} className="ml-2 underline">dismiss</button>
        </div>
      )}

      {/* User list — unified card layout (desktop + mobile) */}
      {(!users || users.length === 0) ? (
        <p className="text-sm text-p-text-light py-4 text-center border border-p-border-light rounded-xl bg-white dark:bg-p-surface">
          No users yet. Add a user to get started.
        </p>
      ) : filteredUsers.length === 0 ? (
        <p className="text-sm text-p-text-light py-4 text-center border border-p-border-light rounded-xl bg-white dark:bg-p-surface">
          No users match “{search.trim()}”.
        </p>
      ) : null}
      <div className="space-y-3">
        {filteredUsers.map((u) => {
          const isEditing = editingSub === u.sub
          const isLocal = (u.auth_provider || 'local').startsWith('local')
          return (
            <div key={u.sub} className="bg-white dark:bg-p-surface rounded-xl border border-p-border-light transition-shadow hover:shadow-xs">
              {/* Header row */}
              <div className="px-4 py-3 flex items-center justify-between gap-3">
                <div className="flex items-center gap-3 min-w-0">
                  {/* Avatar circle */}
                  <div className="w-9 h-9 rounded-full bg-brand/10 flex items-center justify-center shrink-0">
                    <span className="text-sm font-semibold text-brand">
                      {(u.name || u.email)[0]?.toUpperCase()}
                    </span>
                  </div>
                  <div className="min-w-0">
                    <div className="flex items-center gap-1.5 flex-wrap">
                      <p className="font-medium text-sm text-p-text truncate">{u.name}</p>
                      {!!u.is_owner && <span title="Platform owner" className="text-amber-500 text-sm">&#9733;</span>}
                      <span className={`px-1.5 py-0.5 rounded-sm text-[10px] font-medium ${ROLE_BADGE[u.role] || ''}`}>{u.role}</span>
                      {/* Only SSO/OIDC users get an auth badge — "no badge = local". */}
                      {!isLocal && <AuthBadge user={u} />}
                      <AgentsPopover user={u} />
                    </div>
                    <p className="text-xs text-p-text-secondary truncate">{u.email}</p>
                  </div>
                </div>
                <div className="flex items-center gap-2 shrink-0">
                  {!isEditing && (
                    <button onClick={() => startEdit(u)} className="text-xs text-brand hover:text-brand-hover font-medium">Edit</button>
                  )}
                </div>
              </div>

              {/* Expanded content */}
              {isEditing ? (
                <div className="px-4 pb-4 border-t border-p-border-light pt-3">
                  {renderEditForm(u)}
                </div>
              ) : (
                /* Resting body — just status chips + dates. Agents live in the
                   header popover; account actions live in the Edit form. */
                <div className="px-4 pb-3 flex flex-wrap items-center gap-x-4 gap-y-1 text-xs text-p-text-light">
                  {!!u.allow_platform_auth && (
                    <span className="px-1.5 py-0.5 rounded-sm bg-brand/10 text-brand font-medium"
                      title="May borrow admin API credentials when they have no subscription of their own">Platform Auth</span>
                  )}
                  {isLocal && !!u.local_only && (
                    <span className="px-1.5 py-0.5 rounded-sm bg-amber-100 text-amber-700 dark:bg-amber-900/30 dark:text-amber-400 font-medium"
                      title="Login restricted to the local network">LAN only</span>
                  )}
                  {!!u.invite_pending && (
                    <span className="px-1.5 py-0.5 rounded-sm bg-blue-100 text-blue-700 dark:bg-blue-900/30 dark:text-blue-400 font-medium"
                      title="No password set yet — waiting on the invite link (reset the password to issue a new way in)">Invite pending</span>
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
