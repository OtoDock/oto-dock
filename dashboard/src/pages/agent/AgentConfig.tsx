import { useState, useEffect, useCallback, useMemo } from 'react'
import { useParams, useNavigate } from 'react-router-dom'
import { useQueryClient } from '@tanstack/react-query'
import { useAgentInfo, useUpdateAgent, useDeleteAgent, useDelegationTargets, useSetDelegationTargets, useExecutionLayers, useSetDefaultForNewUsers, useKnowledgeAttachments, useKnowledgeLibraries, useSetKnowledgeLibrary, useAttachKnowledgeLibrary, useDetachKnowledgeLibrary, useAgentFiles, useAgentUsers, AgentUpdateError, type AgentUser, type SharedOnlySwitchResult, type KnowledgeLibrary, type FileNode } from '../../api/agents'
import { useRemoteMachines } from '../../api/remoteMachines'
import { PAIRING_SCOPE, TARGET_LOCAL, isLocalTarget } from '../../lib/placement'
import { MACHINE_STATE } from '../../lib/status/machine'
import { useDepartments } from '../../api/departments'
import { wiringSentence } from '../../lib/kinds/department'
import { useAuth } from '../../contexts/AuthContext'
import { ROLE, allowedOnSharedOnly, canManageAgent, isAdmin as isPlatformAdmin, isCreatorOrAbove, roleLabel, type AgentRole } from '../../lib/permissions'
import { tierMarkText, tierTitle } from '../../lib/tiers'
import {
  type VisibilityMode,
  columnsOf,
  isSharedOnly,
  modeOf,
  modeOfAgent,
  MODE_GROUPS,
  MODE_LABEL,
  MODE_OPTION_HINT,
  MODE_SUMMARY,
} from '../../lib/visibility'
import StrongConfirmModal from '../../components/StrongConfirmModal'
import AgentUpdateModal from '../../components/AgentUpdateModal'
import { engineLabel, isCoding, orderedEngines, runsInteractive, runsRemote, vendorBadge } from '../../lib/engines'
import { coerceEffort, effortLabel, effortLadder, offeredEffortLevels, type EffortListing } from '../../lib/engines/effort'
import { Toggle, SavedIndicator, DeleteModal, COLOR_PRESETS } from './AgentConfig.parts'
import { MemorySection } from './AgentConfig.memory'
import { HEAD } from '../../lib/layout/tree'

// ---------------------------------------------------------------------------
// Main component
// ---------------------------------------------------------------------------

// Attachment identity key — agent slugs never contain spaces, so the
// space join is unambiguous even when a subdir does.
const libKey = (source: string, subdir: string) => `${source}\u0000${subdir}`
// The mirror path a library lands at on its consumers.
const libMountPath = (source: string, subdir: string) =>
  subdir ? `knowledge/shared/${source}/${subdir}/` : `knowledge/shared/${source}/`

export default function AgentConfig() {
  const { name } = useParams<{ name: string }>()
  const navigate = useNavigate()
  const { user, refreshUser } = useAuth()
  const { data: info, isLoading } = useAgentInfo(name!)
  const updateAgent = useUpdateAgent()
  // The switch to Shared only has its own mutation: its answer (the removal
  // report, or a 409 with the fresh list) must not be dropped by another save
  // made on the shared instance while it is in flight.
  const switchMode = useUpdateAgent()
  const deleteAgent = useDeleteAgent()
  const { data: delegationData } = useDelegationTargets(name!)
  const setDelegationTargets = useSetDelegationTargets()
  const setDefaultForNewUsers = useSetDefaultForNewUsers()
  const { data: layers } = useExecutionLayers()
  const { data: machines } = useRemoteMachines()
  const { data: departments } = useDepartments()
  const queryClient = useQueryClient()

  const isAdmin = isPlatformAdmin(user)
  // Department assignment is a platform-level org decision: admins + creators
  // only (backend enforces the same gate with a 403 — the row is simply
  // hidden for everyone else, mirroring the Execution Target row).
  const canAssignDepartment = isCreatorOrAbove(user)
  // Knowledge-library wiring is the same platform-role territory: admins +
  // creators mutate; per-agent managers only see the state (the GET below is
  // manager-tier, mutations 403 below admin/creator).
  const canWireKnowledge = isCreatorOrAbove(user)
  const { data: knowledgeData } = useKnowledgeAttachments(name!)
  // The all-libraries feed 403s below admin/creator — only fetch it when
  // this viewer could actually attach one here.
  const { data: knowledgeLibraries } = useKnowledgeLibraries(
    canWireKnowledge && !!name && canManageAgent(user, name),
  )
  const setKnowledgeLibrary = useSetKnowledgeLibrary()
  const attachKnowledgeLibrary = useAttachKnowledgeLibrary()
  const detachKnowledgeLibrary = useDetachKnowledgeLibrary()

  // Local state synced from server. The engine ids come from the agent row
  // (the server always sends a primary); nothing is assumed before it loads.
  const [executionPath, setExecutionPath] = useState('')
  const [executionPaths, setExecutionPaths] = useState<string[]>([])
  const [defaultModel, setDefaultModel] = useState('')
  // Interactive-CLI per-agent default execution mode: '' (unset
  // → platform default), 'interactive', or '-p'. Only shown when the default
  // model runs on an engine with a native TUI (runtime.supports_interactive_pty).
  const [defaultExecutionMode, setDefaultExecutionMode] = useState<'' | 'interactive' | '-p'>('')
  const [defaultEffort, setDefaultEffort] = useState('')
  // Whether this viewer may write the agent (the reconcile effects below
  // save; an editor's or viewer's copy must only read).
  const canManage = !!name && canManageAgent(user, name)
  // Visibility mode = (collaborative × default_scope). The two columns are
  // stored independently; the UI presents them as one of four named modes.
  // `default_scope` still drives tasks/notifications/triggers/meetings/memory.
  const [collaborative, setCollaborative] = useState(true)
  const [defaultScope, setDefaultScope] = useState<'user' | 'agent'>('user')
  // Pending mode awaiting a type-to-confirm (set only for into/out-of Shared
  // only flips, which reshuffle which chats a user sees).
  const [pendingMode, setPendingMode] = useState<VisibilityMode | null>(null)
  // The switch INTO Shared only removes the viewer and contributor rows: the
  // list the server answered with (a 409 when the live rows changed), the
  // error to show in the dialog, and what the switch reported once done.
  const [switchPeople, setSwitchPeople] = useState<AgentUser[] | null>(null)
  const [switchError, setSwitchError] = useState<string | null>(null)
  const [switchNotice, setSwitchNotice] = useState<string | null>(null)
  const [dfnuError, setDfnuError] = useState<string | null>(null)
  const [adminOnly, setAdminOnly] = useState(false)
  const [showTemplateUpdate, setShowTemplateUpdate] = useState(false)
  const [agentColor, setAgentColor] = useState('')
  const [displayName, setDisplayName] = useState('')
  const [description, setDescription] = useState('')
  const [savedField, setSavedField] = useState<string | null>(null)
  const [deleteModalOpen, setDeleteModalOpen] = useState(false)
  const [executionTarget, setExecutionTarget] = useState<string>(TARGET_LOCAL)
  // Department assignment ('' = unassigned). The two ids always travel
  // together — see saveDepartment.
  const [departmentId, setDepartmentId] = useState('')
  const [departmentLevelId, setDepartmentLevelId] = useState('')
  const [selectedTargets, setSelectedTargets] = useState<Set<string>>(new Set())
  // Default-for-new-users — admin only. Empty role = disabled.
  const [dfnuEnabled, setDfnuEnabled] = useState(false)
  const [dfnuRole, setDfnuRole] = useState<AgentRole>(ROLE.VIEWER)
  // Shared knowledge — per-library state. Attachment identity is the
  // (source, subdir) pair, so optimistic-writable keys join both.
  const [shareFormOpen, setShareFormOpen] = useState(false)
  const [libraryNameDraft, setLibraryNameDraft] = useState('')
  const [librarySubdirDraft, setLibrarySubdirDraft] = useState('')
  // Set while renaming an existing library — locks the subfolder field so
  // the PUT hits the same (source, subdir) row.
  const [renamingSubdir, setRenamingSubdir] = useState<string | null>(null)
  // Server-side validation surfaces here (bad subdir, overlap, bulletin
  // rename conflicts) — the backend messages are the source of truth.
  const [shareError, setShareError] = useState('')
  const [attachmentWritable, setAttachmentWritable] = useState<Record<string, boolean>>({})
  const [attachPick, setAttachPick] = useState('')   // JSON [source, subdir]
  const [attachWritable, setAttachWritable] = useState(false)
  const [unshareTarget, setUnshareTarget] = useState<KnowledgeLibrary | null>(null)

  useEffect(() => {
    if (!info) return
    setExecutionPath(info.execution_path || '')
    setExecutionPaths(info.execution_paths || (info.execution_path ? [info.execution_path] : []))
    setDefaultModel(info.default_model || '')
    setDefaultExecutionMode(info.default_execution_mode || '')
    setDefaultEffort(info.default_effort || '')
    setCollaborative(info.collaborative ?? true)
    setDefaultScope(info.default_scope || 'user')
    setAdminOnly(info.admin_only ?? false)
    setAgentColor(info.color || '')
    setDisplayName(info.display_name || '')
    setDescription(info.description || '')
    setExecutionTarget(info.execution_target || TARGET_LOCAL)
    setDepartmentId(info.department_id || '')
    setDepartmentLevelId(info.department_level_id || '')
    const dfnuRoleRaw = info.default_for_new_users_role
    setDfnuEnabled(!!dfnuRoleRaw)
    // Off: the role the toggle would turn on with (editor on a Shared-only
    // agent, whose rows take editor or above).
    setDfnuRole(dfnuRoleRaw || (isSharedOnly(modeOfAgent(info)) ? ROLE.EDITOR : ROLE.VIEWER))
  }, [info])

  useEffect(() => {
    if (knowledgeData) {
      setAttachmentWritable(Object.fromEntries(
        knowledgeData.attachments.map(
          (a) => [libKey(a.source_agent, a.subdir), a.writable]),
      ))
    }
  }, [knowledgeData])

  useEffect(() => {
    if (delegationData) {
      // selectedTargets holds MANUAL targets only — it's the PUT payload.
      // Department-compiled targets are locked rows the compiler owns; if an
      // agent somehow appears in both lists (defensive), compiled wins so a
      // toggle round-trip can never re-submit it as manual.
      const locked = new Set((delegationData.compiled ?? []).map((e) => e.target))
      setSelectedTargets(new Set(delegationData.targets.filter((t) => !locked.has(t))))
    }
  }, [delegationData])

  const save = useCallback(
    (field: string, value: any) => {
      updateAgent.mutate(
        { name: name!, [field]: value },
        {
          onSuccess: () => {
            setSavedField(field)
            setTimeout(() => setSavedField(null), 1500)
          },
        },
      )
    },
    [name, updateAgent],
  )

  // Current visibility mode + a one-PATCH writer that persists both columns
  // together (so the agent never lands in a transient half-applied state).
  const mode = modeOf(collaborative, defaultScope)
  const saveMode = useCallback(
    (next: VisibilityMode) => {
      const cols = columnsOf(next)
      setCollaborative(cols.collaborative)
      setDefaultScope(cols.default_scope)
      updateAgent.mutate(
        { name: name!, collaborative: cols.collaborative, default_scope: cols.default_scope },
        {
          onSuccess: () => {
            setSavedField('visibility_mode')
            setTimeout(() => setSavedField(null), 1500)
          },
        },
      )
    },
    [name, updateAgent],
  )

  // Department assignment — a one-PATCH writer like saveMode: the backend
  // contract is that department_id and department_level_id always ship
  // TOGETHER (a lone id would leave the agent half-assigned). On success the
  // server recompiles department membership + delegation edges, so refetch
  // both feeds.
  const saveDepartment = useCallback(
    (deptId: string, levelId: string) => {
      setDepartmentId(deptId)
      setDepartmentLevelId(levelId)
      updateAgent.mutate(
        { name: name!, department_id: deptId, department_level_id: levelId },
        {
          onSuccess: () => {
            setSavedField('department')
            setTimeout(() => setSavedField(null), 1500)
            queryClient.invalidateQueries({ queryKey: ['departments'] })
            queryClient.invalidateQueries({ queryKey: ['delegation-targets', name] })
          },
        },
      )
    },
    [name, updateAgent, queryClient],
  )

  // Folder picker feed for the share form: the agent's knowledge/ subtree,
  // directories only. `knowledge/shared/` (consumer mirrors) and `memory`
  // segments are excluded — the server rejects them; hiding beats a 400.
  // Only fetched while the form is open (the tree GET is manager-tier).
  const { data: shareFileTree } = useAgentFiles(shareFormOpen ? name! : '')
  const knowledgeFolders = useMemo(() => {
    const root = (shareFileTree ?? []).find(
      (n) => n.type === 'dir' && n.name === HEAD.KNOWLEDGE)
    const out: { path: string; depth: number }[] = []
    const walk = (nodes: FileNode[], prefix: string, depth: number) => {
      for (const n of nodes) {
        if (n.type !== 'dir') continue
        if (depth === 0 && n.name === 'shared') continue
        if (n.name === 'memory') continue
        const rel = prefix ? `${prefix}/${n.name}` : n.name
        out.push({ path: rel, depth })
        if (n.children) walk(n.children, rel, depth + 1)
      }
    }
    if (root?.children) walk(root.children, '', 0)
    return out
  }, [shareFileTree])
  // A folder is un-shareable when an existing library already covers it or
  // would be covered by it (the server enforces disjoint subtrees). '' (the
  // whole folder) overlaps everything.
  const shareOverlap = useCallback(
    (path: string): string | null => {
      for (const l of knowledgeData?.libraries ?? []) {
        if (renamingSubdir !== null && l.subdir === renamingSubdir) continue
        const covers = l.subdir === '' || path === l.subdir
          || (l.subdir !== '' && path.startsWith(`${l.subdir}/`))
          || (path !== '' && l.subdir.startsWith(`${path}/`))
          || path === ''
        if (covers) return l.name || l.subdir || 'whole folder'
      }
      return null
    },
    [knowledgeData, renamingSubdir],
  )
  // Opening the form preselects the whole folder; when a library already
  // covers the current pick, fall to the first free folder once the tree
  // loads (same-value sets bail out, so this can't loop).
  useEffect(() => {
    if (!shareFormOpen || renamingSubdir !== null) return
    if (!shareOverlap(librarySubdirDraft)) return
    const free = ['', ...knowledgeFolders.map((f) => f.path)]
      .find((p) => !shareOverlap(p))
    setLibrarySubdirDraft(free ?? '')
  }, [shareFormOpen, renamingSubdir, knowledgeFolders, shareOverlap, librarySubdirDraft])

  // Share (or rename — same PUT) one library: a subtree of this agent's
  // knowledge folder ('' = the whole folder). Server-side validation
  // (subdir shape, overlap, bulletin rename conflicts) lands in shareError.
  const shareLibrary = useCallback(
    (subdir: string, libraryName: string) => {
      setShareError('')
      setKnowledgeLibrary.mutate(
        { agent: name!, enabled: true, name: libraryName, subdir },
        {
          onSuccess: () => {
            setShareFormOpen(false)
            setRenamingSubdir(null)
            setLibraryNameDraft('')
            setLibrarySubdirDraft('')
            setSavedField('shared_knowledge')
            setTimeout(() => setSavedField(null), 1500)
          },
          onError: (e) => setShareError(
            e instanceof Error ? e.message : 'Failed to share library'),
        },
      )
    },
    [name, setKnowledgeLibrary],
  )

  // Un-share ONE library. With consumers attached the caller routes through
  // a type-to-confirm first (unshareTarget) — the server detaches them all
  // and tears their mirror subtrees down (v1 warns, never blocks).
  const unshareLibrary = useCallback(
    (subdir: string) => {
      setKnowledgeLibrary.mutate(
        { agent: name!, enabled: false, subdir },
        {
          onSuccess: () => {
            setSavedField('shared_knowledge')
            setTimeout(() => setSavedField(null), 1500)
          },
        },
      )
    },
    [name, setKnowledgeLibrary],
  )

  // Flipping into OR out of Shared only changes chat-history grouping (one
  // shared list ↔ per-user lists), so it gets a type-to-confirm. Every other
  // transition saves immediately.
  const onSelectMode = (next: VisibilityMode) => {
    if (next === mode) return
    const crossesShared = (mode === 'shared_only') !== (next === 'shared_only')
    setSwitchNotice(null)
    if (crossesShared) {
      setSwitchPeople(null)
      setSwitchError(null)
      setPendingMode(next)
    } else saveMode(next)
  }

  // Shared only takes the editor role to chat: switching into it removes the
  // viewer and contributor rows, which the dialog names (the users list, read
  // while it is open) and the PATCH confirms exactly. Saved only when the
  // server agrees: a 409 means the rows changed, and the dialog shows the
  // fresh list to confirm again.
  const { data: agentUsers, isLoading: usersLoading, isError: usersFailed } = useAgentUsers(name || '', {
    enabled: pendingMode === 'shared_only' && canManage,
  })
  // The server's list, exactly: an admin's row is inert (they act as admin)
  // and is neither named nor removed.
  const losing = switchPeople ?? (agentUsers || [])
    .filter((p) => !allowedOnSharedOnly(p.role) && !isPlatformAdmin(p.platform_role))
  // Until the list is read the dialog cannot say who loses access; a failed
  // read may still confirm (the server answers with the list to check).
  const listPending = pendingMode === 'shared_only' && !switchPeople && usersLoading
  const confirmSharedOnly = () => {
    const cols = columnsOf('shared_only')
    setSwitchError(null)
    switchMode.mutate(
      { name: name!, collaborative: cols.collaborative, default_scope: cols.default_scope,
        confirm_removals: losing.map((p) => p.sub) },
      {
        onSuccess: (row: { shared_only_switch?: SharedOnlySwitchResult }) => {
          setCollaborative(cols.collaborative)
          setDefaultScope(cols.default_scope)
          setPendingMode(null)
          setSwitchPeople(null)
          const r = row?.shared_only_switch
          const notes: string[] = []
          if (r?.not_removed?.length) {
            notes.push(`${r.not_removed.length} viewer or contributor assignment(s) were kept `
              + '(added during the switch, or not removed): an admin changes them in Admin → Users.')
          }
          if (r?.default_cleared) notes.push('The role new users are attached with was cleared.')
          setSwitchNotice(notes.length ? notes.join(' ') : null)
          queryClient.invalidateQueries({ queryKey: ['agent-users', name] })
          queryClient.invalidateQueries({ queryKey: ['admin-users'] })
          if (user?.sub && r?.removed?.includes(user.sub)) void refreshUser()
          setSavedField('visibility_mode')
          setTimeout(() => setSavedField(null), 1500)
        },
        onError: (e) => {
          const fresh = e instanceof AgentUpdateError ? e.sharedOnlyPeople : null
          if (fresh) {
            setSwitchPeople(fresh)
            setSwitchError('Check who loses access and confirm again: the list above is the current one.')
          } else {
            setSwitchError(e.message)
          }
        },
      },
    )
  }

  // The agent's enabled engines, in the catalog's engine order.
  const enabledEngines = orderedEngines(layers).filter(e => executionPaths.includes(e.name))
  // The catalog has answered: the effort and mode effects below may
  // reconcile a stored value against it. Before that every list is empty
  // and a "not offered" verdict would be a guess that gets SAVED.
  const catalogLoaded = layers !== undefined && enabledEngines.length > 0
  // Grouped, ordered options for the Default Model picker — one <optgroup>
  // per enabled engine. The blank option = "auto", which the SERVER resolves
  // (see autoModel below).
  const modelGroups = enabledEngines
    .map(e => ({
      path: e.name,
      label: engineLabel(e),
      // Drop the per-engine "System Default" placeholder (empty value) — the
      // top-level Auto option already covers "let the platform decide".
      // Tier order inside an engine: the list a person scans is the ranking.
      models: e.models
        .filter(m => m.value && m.label !== 'System Default')
        .map((m, i) => ({ m, i }))
        .sort((a, b) => (a.m.tier || 99) - (b.m.tier || 99) || a.i - b.i)
        .map(({ m }) => m),
    }))
    .filter(g => g.models.length > 0)
  // What "Auto" runs: the PRIMARY engine's resolved default as the catalog
  // serves it (`auto_model` — the declared default, or the fall-down when it
  // is disabled or the pool cannot serve it), with its display name. Rendered,
  // never re-derived: the old "first model of the first engine" guess said
  // "Fable 5.1" for an agent that ran Opus 5. The served list's row supplies
  // the XHigh/ultra flags when it carries the model; a model the list
  // filtered out (the catalog's provider filter is not the resolver's) still
  // gets its name and no flags.
  const primaryEngine = layers?.[executionPath]
  const autoModelId = primaryEngine?.auto_model || ''
  const autoModel = autoModelId
    ? primaryEngine?.models.find(m => m.value === autoModelId)
      ?? { value: autoModelId, label: primaryEngine?.auto_model_label || autoModelId }
    : undefined

  // Interactive-CLI per-agent default-mode control visibility: only when the
  // agent's DEFAULT model runs on an engine with a native TUI
  // (`runtime.supports_interactive_pty`) THAT THIS AGENT USES. An engine
  // without one has nothing to run interactively, so the control is hidden
  // (mirrors the resolver + back-end gate). The engine intersection matters:
  // a local model served by two engines is listed under both, which used to
  // show the control on an agent whose only engine has no TUI (live-hit
  // 2026-09-07).
  const defaultModelIsCliLayer = !!defaultModel && enabledEngines.some(e =>
    runsInteractive(e) && e.models.some(m => m.value === defaultModel)
  )

  // A stored interactive default that the control no longer offers (the
  // agent moved to Direct LLM, or the model changed) is reset — the resolver
  // would run -p anyway, but the row must not keep claiming otherwise.
  useEffect(() => {
    if (catalogLoaded && canManage && defaultExecutionMode && defaultModel && !defaultModelIsCliLayer) {
      setDefaultExecutionMode('')
      save('default_execution_mode', '')
    }
  }, [catalogLoaded, canManage, defaultExecutionMode, defaultModel, defaultModelIsCliLayer, save])

  // The Default Effort ladder is the union of the enabled engines' declared
  // levels, trimmed to what the chosen model's provider takes and gated per
  // model where the provider says the row's flag decides (lib/effort). The
  // listings are every (engine, row) pair that carries the model — the same
  // model can sit under two engines, and either's offer counts. Auto ('')
  // lists the primary engine's resolved row when the served list has it; the
  // flagless fallback of the "Auto — label" option is a name, not a listing.
  const effortLadderLevels = effortLadder(enabledEngines)
  const effortListings: EffortListing[] = defaultModel
    ? enabledEngines.flatMap(e => e.models.filter(m => m.value === defaultModel).map(model => ({ engine: e, model })))
    : (() => {
        const hit = primaryEngine && autoModelId ? primaryEngine.models.find(m => m.value === autoModelId) : undefined
        return hit && primaryEngine ? [{ engine: primaryEngine, model: hit }] : []
      })()
  const offeredEfforts = offeredEffortLevels(effortLadderLevels, effortListings, enabledEngines)
  // What the select shows: the stored value when it is offered, else the
  // nearest offered level below it (a stored xhigh on a model without the
  // flag shows High, a stored max on a provider whose ladder tops at xhigh
  // shows XHigh — the level the wire already sent).
  const shownEffort = coerceEffort(defaultEffort, offeredEfforts, effortLadderLevels)

  // A stored effort the control no longer offers is written back as what it
  // shows, so the dropdown and the DB never disagree — only once the catalog
  // has answered (an empty list before it loads is not "not offered").
  useEffect(() => {
    if (!catalogLoaded || !canManage || !defaultEffort || shownEffort === defaultEffort) return
    setDefaultEffort(shownEffort)
    save('default_effort', shownEffort)
  }, [catalogLoaded, canManage, defaultEffort, shownEffort, save])

  const handleDelete = () => {
    deleteAgent.mutate(
      { name: name!, confirm_slug: name! },
      { onSuccess: () => navigate('/agents') },
    )
  }

  if (isLoading) return <p className="text-sm text-p-text-secondary">Loading...</p>

  const agentRole = name ? user?.agent_roles?.[name] : undefined
  const isReadOnly = !canManage  // editor + viewer: read-only

  // Department-compiled delegation edges, keyed by target. Rendered as
  // locked (checked + disabled) rows and NEVER included in the PUT payload —
  // the department compiler owns those edges server-side.
  const compiledByTarget = new Map(
    (delegationData?.compiled ?? []).map((e) => [e.target, e] as const),
  )

  // Shared-knowledge mutation gate: platform admin/creator AND page-editable
  // (a creator who doesn't manage this agent is read-only here, matching the
  // backend's manager requirement for creators).
  const canMutateKnowledge = canWireKnowledge && !isReadOnly
  // Libraries this agent could still attach: every promoted library minus
  // its own and the (source, subdir) pairs already attached.
  const attachedPairs = new Set(
    (knowledgeData?.attachments ?? []).map((a) => libKey(a.source_agent, a.subdir)),
  )
  const attachableLibraries = (knowledgeLibraries ?? []).filter(
    (l) => l.source_agent !== name && !attachedPairs.has(libKey(l.source_agent, l.subdir)),
  )

  return (
    <div className="space-y-6">
      {isReadOnly && (
        <div className="bg-amber-50 dark:bg-amber-900/20 border border-amber-200 dark:border-amber-700 rounded-xl px-4 py-3 text-xs text-amber-800 dark:text-amber-200">
          <strong>Read-only.</strong> Agent settings are owner-only.{' '}
          {agentRole === ROLE.EDITOR
            ? 'As an editor you can collaborate on the agent\'s shared workspace and files; agent behavior (prompt, MCPs, knowledge, default scope) is curated by an owner.'
            : agentRole === ROLE.CONTRIBUTOR
              ? 'As a contributor you can add and edit files in the agent\'s shared workspace; agent behavior (prompt, MCPs, knowledge, default scope) is curated by an owner.'
            : agentRole === ROLE.VIEWER
              ? 'As a viewer you can read the agent\'s workspace, knowledge, and config; only owners can change them.'
              : 'You do not have manager access to this agent.'}
        </div>
      )}
      {canManage && info?.template_update?.update_available && (
        <div
          className="bg-brand/5 border border-brand/30 rounded-xl px-4 py-3 text-xs text-p-text flex flex-wrap items-center justify-between gap-2"
          data-testid="template-update-banner"
        >
          <span>
            <strong>Version {info.template_update.catalog_version}</strong> of this agent's template is available
            {info.template_update.installed_version ? ` (installed: ${info.template_update.installed_version})` : ''}.
            {!info.template_update.compat_ok ? ' It needs a newer OtoDock.' : ' What you changed stays; the update shows what it replaces before it runs.'}
          </span>
          <button
            onClick={() => setShowTemplateUpdate(true)}
            disabled={!info.template_update.compat_ok}
            className="px-3 py-1.5 rounded-lg text-xs font-medium text-white bg-brand hover:bg-brand-hover transition-colors disabled:opacity-50"
          >
            Update
          </button>
        </div>
      )}
      {showTemplateUpdate && name && (
        <AgentUpdateModal open agentSlug={name} onClose={() => setShowTemplateUpdate(false)} />
      )}
      {/* Settings */}
      <div className="bg-white dark:bg-p-surface rounded-xl border border-p-border-light p-4">
        <p className="text-xs font-semibold text-p-text-secondary uppercase mb-4">Agent Configuration</p>
        {/* divide-y draws a subtle separator between every setting; the child
            padding utilities give each row consistent breathing room. */}
        <div className="divide-y divide-p-border-light [&>*]:py-4 [&>*:first-child]:pt-0 [&>*:last-child]:pb-0">
          {/* Display Name */}
          <div className="flex flex-col gap-2 sm:flex-row sm:items-center sm:justify-between sm:gap-4">
            <div>
              <p className="text-sm font-medium text-p-text">Display Name</p>
              <p className="text-xs text-p-text-light">Human-readable agent name</p>
            </div>
            <div className="flex items-center gap-2">
              <input
                type="text"
                value={displayName}
                onChange={(e) => setDisplayName(e.target.value)}
                onBlur={() => {
                  if (displayName && displayName !== info?.display_name) {
                    save('display_name', displayName)
                  }
                }}
                onKeyDown={(e) => {
                  if (e.key === 'Enter') {
                    (e.target as HTMLInputElement).blur()
                  }
                }}
                className="px-2.5 py-1.5 text-sm border border-p-border-light rounded-lg bg-p-bg text-p-text focus:outline-hidden focus:ring-2 focus:ring-brand/30 w-48"
              />
              <SavedIndicator show={savedField === 'display_name'} />
            </div>
          </div>

          {/* Slug (read-only) */}
          <div className="flex flex-col gap-2 sm:flex-row sm:items-center sm:justify-between sm:gap-4">
            <div>
              <p className="text-sm font-medium text-p-text">Slug</p>
              <p className="text-xs text-p-text-light">Unique identifier (read-only)</p>
            </div>
            <span className="px-2.5 py-1.5 text-sm text-p-text-secondary font-mono bg-p-surface rounded-lg border border-p-border-light">
              {name}
            </span>
          </div>

          {/* Template (read-only): the community template the agent came
              from and the version it runs — always, not only when an update
              exists, so a manager knows what they have. */}
          {info?.community_template && (
            <div
              className="flex flex-col gap-2 sm:flex-row sm:items-center sm:justify-between sm:gap-4"
              data-testid="template-row"
            >
              <div>
                <p className="text-sm font-medium text-p-text">Template</p>
                <p className="text-xs text-p-text-light">
                  The community template this agent came from. A newer version shows above when the catalog has one.
                </p>
              </div>
              <span className="px-2.5 py-1.5 text-sm text-p-text-secondary font-mono bg-p-surface rounded-lg border border-p-border-light">
                {info.community_template.startsWith('local:') ? 'local template' : info.community_template}
                {' · '}
                {info.community_template_version ? `version ${info.community_template_version}` : 'version unknown'}
              </span>
            </div>
          )}

          {/* Description */}
          <div className="flex flex-col gap-2">
            <div className="flex items-center justify-between">
              <div>
                <p className="text-sm font-medium text-p-text">Description</p>
                <p className="text-xs text-p-text-light">What this agent does — shown on cards and to other agents</p>
              </div>
              <SavedIndicator show={savedField === 'description'} />
            </div>
            <textarea
              value={description}
              onChange={(e) => setDescription(e.target.value)}
              onBlur={() => {
                if (description !== (info?.description || '')) {
                  save('description', description)
                }
              }}
              rows={2}
              placeholder="e.g., Manages smart home devices, cameras, and automations"
              className="w-full px-2.5 py-1.5 text-sm border border-p-border-light rounded-lg bg-p-bg text-p-text focus:outline-hidden focus:ring-2 focus:ring-brand/30 resize-none"
            />
          </div>

          {/* Agent Color */}
          <div className="flex flex-col gap-2 sm:flex-row sm:items-center sm:justify-between sm:gap-4">
            <div>
              <p className="text-sm font-medium text-p-text">Agent Color</p>
              <p className="text-xs text-p-text-light">Color for cards and chat avatar</p>
            </div>
            <div className="flex items-center gap-2">
              <div className="flex gap-1.5 flex-wrap justify-end">
                {COLOR_PRESETS.map(({ hex, name }) => (
                  <button
                    key={hex}
                    title={name}
                    onClick={() => {
                      setAgentColor(hex)
                      save('color', hex)
                    }}
                    className={`w-6 h-6 rounded-full border-2 transition-all ${
                      agentColor === hex
                        ? 'border-p-text scale-110'
                        : 'border-transparent hover:scale-105'
                    }`}
                    style={{ backgroundColor: hex }}
                  />
                ))}
              </div>
              <SavedIndicator show={savedField === 'color'} />
            </div>
          </div>

          {/* Execution Path */}
          <div className="flex flex-col gap-3">
            <div className="flex items-center justify-between gap-3">
              <div>
                <p className="text-sm font-medium text-p-text">AI Engines</p>
                <p className="text-xs text-p-text-light">Which AI engines this agent can use</p>
              </div>
              <SavedIndicator show={savedField === 'execution_paths' || savedField === 'execution_path'} />
            </div>
            {/* Click-to-toggle cards (like the visibility options) — full-width,
                mobile-friendly, with a provider badge per engine. */}
            <div className="space-y-2">
              {orderedEngines(layers).map((engine) => {
                const path = engine.name
                const badge = vendorBadge(engine)
                const label = engineLabel(engine)
                const checked = executionPaths.includes(path)
                // Platform-level gate (server mirror: PATCH /v1/agents refuses
                // newly-added unconfigured engines). `!== false` so a loading
                // list or an older proxy (field absent) never flashes a locked
                // card. Already-enabled engines stay uncheckable-only when
                // their platform subscription vanished (grandfathering);
                // admins toggle freely (they're about to connect one).
                const configured = engine.configured !== false
                const lockedOff = !checked && !configured && !isAdmin
                return (
                  <button
                    type="button"
                    key={path}
                    disabled={isReadOnly || lockedOff}
                    onClick={() => {
                      let next: string[]
                      if (checked) {
                        next = executionPaths.filter(p => p !== path)
                        if (next.length === 0) return // at least one required
                      } else {
                        next = [...executionPaths, path]
                      }
                      setExecutionPaths(next)
                      setExecutionPath(next[0])
                      // Reset model if it's not available in any selected engine.
                      const allModels = next.flatMap(p => layers?.[p]?.models || [])
                      if (!allModels.some((m: { value: string }) => m.value === defaultModel)) {
                        setDefaultModel('')
                        save('default_model', '')
                      }
                      save('execution_paths', next)
                    }}
                    className={`w-full flex items-start gap-2.5 rounded-lg border px-3 py-2 text-left transition-colors ${
                      checked ? 'border-brand bg-brand-surface' : 'border-p-border-light hover:bg-p-surface-hover'
                    } ${isReadOnly || lockedOff ? 'cursor-not-allowed opacity-60' : 'cursor-pointer'}`}
                  >
                    <span className={`mt-0.5 w-4 h-4 shrink-0 rounded-sm border flex items-center justify-center ${
                      checked ? 'bg-brand border-brand text-white' : 'border-p-border-light'
                    }`}>
                      {checked && (
                        <svg className="w-3 h-3" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                          <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={3} d="M5 13l4 4L19 7" />
                        </svg>
                      )}
                    </span>
                    <span className="min-w-0">
                      <span className="flex items-center gap-1.5 flex-wrap">
                        {badge && (
                          <span className="text-[10px] font-semibold uppercase tracking-wide px-1.5 py-0.5 rounded-sm bg-p-bg text-p-text-secondary border border-p-border-light">
                            {badge}
                          </span>
                        )}
                        <span className="text-sm font-medium text-p-text">{label}</span>
                      </span>
                      {!isCoding(engine) && (
                        <span className="block text-xs text-p-text-light mt-0.5">
                          Supporting engine — fewer tools, lower latency.
                        </span>
                      )}
                      {!configured && !checked && (
                        <span className="block text-xs text-amber-600 dark:text-amber-500 mt-0.5">
                          No {label} subscription is connected on this platform
                          {isAdmin ? ' — connect one in Platform → AI Engines' : ''}
                        </span>
                      )}
                    </span>
                  </button>
                )
              })}
            </div>
          </div>

          {/* Execution Target — admins only. Non-admins can't set a remote
              target (backend enforces admin + admin-paired on save), so the
              control is hidden rather than shown-then-rejected. Also hidden
              when this build ships without the remote-machines feature. */}
          {isAdmin && user?.feature_flags?.remote_machines_available !== false && (
          <div className="flex flex-col gap-2 sm:flex-row sm:items-center sm:justify-between sm:gap-4">
            <div>
              <p className="text-sm font-medium text-p-text">Execution Target</p>
              <p className="text-xs text-p-text-light">
                {!runsRemote(primaryEngine)
                  ? 'This engine always runs on the platform host'
                  : 'Where this agent runs — local or on a remote machine via satellite'}
              </p>
            </div>
            {/* Mobile: the select takes the full row (min-w-0 lets it shrink
                below its widest option) and the status badge wraps BELOW it;
                from sm: up the badge sits beside an auto-width select. */}
            <div className="flex flex-wrap items-center gap-2 min-w-0">
              {!runsRemote(primaryEngine) ? (
                <span className="text-sm text-p-text-light">Local only</span>
              ) : (
                <select
                  value={executionTarget}
                  onChange={e => {
                    setExecutionTarget(e.target.value)
                    save('execution_target', e.target.value)
                  }}
                  className="w-full min-w-0 sm:w-auto sm:max-w-xs px-2.5 py-1.5 text-sm border border-p-border-light rounded-lg bg-p-bg text-p-text focus:outline-hidden focus:ring-2 focus:ring-brand/30"
                >
                  <option value={TARGET_LOCAL}>Local (this server)</option>
                  {(machines ?? []).filter(m => m.pairing_scope === PAIRING_SCOPE.ADMIN).map(m => (
                    <option key={m.id} value={m.id}>
                      {m.name} {m.status === MACHINE_STATE.ONLINE ? '' : `[${m.status}]`}
                    </option>
                  ))}
                </select>
              )}
              {!isLocalTarget(executionTarget) && (
                <span className={`inline-flex items-center px-2 py-0.5 rounded-sm text-xs font-medium ${
                  (machines ?? []).find(m => m.id === executionTarget)?.status === MACHINE_STATE.ONLINE
                    ? 'bg-green-100 text-green-700 dark:bg-green-900/30 dark:text-green-400'
                    : 'bg-amber-100 text-amber-700 dark:bg-amber-900/30 dark:text-amber-400'
                }`}>
                  {(machines ?? []).find(m => m.id === executionTarget)?.status ?? 'unknown'}
                </span>
              )}
              <SavedIndicator show={savedField === 'execution_target'} />
            </div>
          </div>
          )}

          {/* Department — admins + creators only (backend 403s everyone
              else; hidden rather than shown-then-rejected, like the
              Execution Target row). Both ids always save together in one
              PATCH via saveDepartment. */}
          {canAssignDepartment && (
          <div className="flex flex-col gap-2 sm:flex-row sm:items-center sm:justify-between sm:gap-4">
            <div>
              <p className="text-sm font-medium text-p-text">Department</p>
              <p className="text-xs text-p-text-light">
                {(() => {
                  // The selected department's own wiring, so the row says
                  // what the locked "via" rows below will contain.
                  const dept = (departments ?? []).find((d) => d.id === departmentId)
                  if (!dept) return 'Auto-wires delegation within the department, following its delegation mode and reach.'
                  return wiringSentence(dept.mode, dept.reach)
                })()}
              </p>
            </div>
            <div className="flex items-center gap-2">
              <select
                aria-label="Department"
                value={departmentId}
                disabled={isReadOnly}
                onChange={(e) => {
                  const deptId = e.target.value
                  if (!deptId) {
                    // Unassign: both ids clear together (backend contract).
                    saveDepartment('', '')
                    return
                  }
                  // Picking a department auto-picks its top level (rank
                  // order) so the pair is always complete in one PATCH.
                  const dept = (departments ?? []).find((d) => d.id === deptId)
                  const firstLevel = [...(dept?.levels ?? [])].sort((a, b) => a.rank - b.rank)[0]
                  saveDepartment(deptId, firstLevel?.id || '')
                }}
                className="px-2.5 py-1.5 text-sm border border-p-border-light rounded-lg bg-p-bg text-p-text focus:outline-hidden focus:ring-2 focus:ring-brand/30 disabled:cursor-not-allowed"
              >
                <option value="">None</option>
                {(departments ?? []).map((d) => (
                  <option key={d.id} value={d.id}>{d.name}</option>
                ))}
              </select>
              {departmentId && (
                <select
                  aria-label="Level"
                  value={departmentLevelId}
                  disabled={isReadOnly}
                  onChange={(e) => saveDepartment(departmentId, e.target.value)}
                  className="px-2.5 py-1.5 text-sm border border-p-border-light rounded-lg bg-p-bg text-p-text focus:outline-hidden focus:ring-2 focus:ring-brand/30 disabled:cursor-not-allowed"
                >
                  {[...((departments ?? []).find((d) => d.id === departmentId)?.levels ?? [])]
                    .sort((a, b) => a.rank - b.rank)
                    .map((l) => (
                      <option key={l.id} value={l.id}>{l.name}</option>
                    ))}
                </select>
              )}
              <SavedIndicator show={savedField === 'department'} />
            </div>
          </div>
          )}

          {/* Default Model */}
          <div className="flex flex-col gap-2 sm:flex-row sm:items-center sm:justify-between sm:gap-4">
            <div>
              <p className="text-sm font-medium text-p-text">Default Model</p>
              <p className="text-xs text-p-text-light">Model used when no override is set</p>
            </div>
            <div className="flex items-center gap-2">
              <select
                value={defaultModel}
                onChange={(e) => {
                  setDefaultModel(e.target.value)
                  save('default_model', e.target.value)
                }}
                className="w-full sm:w-auto sm:max-w-xs px-2.5 py-1.5 text-sm border border-p-border-light rounded-lg bg-p-bg text-p-text focus:outline-hidden focus:ring-2 focus:ring-brand/30"
              >
                <option value="">{autoModel ? `Auto — ${autoModel.label}` : 'Auto'}</option>
                {modelGroups.map(g => (
                  <optgroup key={g.path} label={g.label}>
                    {g.models.map(m => (
                      <option key={m.value} value={m.value} title={m.tier || m.good_at ? tierTitle(m.tier, m.tier_label, m.good_at) : undefined}>
                        {m.label}{m.tier ? `  ${tierMarkText(m.tier)}` : ''}
                      </option>
                    ))}
                  </optgroup>
                ))}
              </select>
              <SavedIndicator show={savedField === 'default_model'} />
            </div>
          </div>

          {/* Default Session Mode (Interactive CLI) — only when the
              default model runs on an engine with a native TUI (its
              descriptor's supports_interactive_pty), and only when the
              platform-wide interactive kill-switch is on. Manager-gated
              server-side (PATCH /v1/agents requires manage). */}
          {defaultModelIsCliLayer && user?.feature_flags?.interactive_terminal_enabled !== false && (
            <div className="flex flex-col gap-2 sm:flex-row sm:items-center sm:justify-between sm:gap-4">
              <div>
                <p className="text-sm font-medium text-p-text">Default Session Mode</p>
                <p className="text-xs text-p-text-light">
                  How new chats &amp; tasks start — the normal headless stream, or the
                  interactive terminal (the native CLI running as a live TUI)
                </p>
              </div>
              <div className="flex items-center gap-2">
                <select
                  value={defaultExecutionMode || '-p'}
                  onChange={(e) => {
                    const v = e.target.value as 'interactive' | '-p'
                    setDefaultExecutionMode(v)
                    save('default_execution_mode', v)
                  }}
                  className="px-2.5 py-1.5 text-sm border border-p-border-light rounded-lg bg-p-bg text-p-text focus:outline-hidden focus:ring-2 focus:ring-brand/30"
                >
                  <option value="-p">Normal (headless)</option>
                  <option value="interactive">Interactive terminal</option>
                </select>
                <SavedIndicator show={savedField === 'default_execution_mode'} />
              </div>
            </div>
          )}

          {/* Default Effort — the options are the enabled engines' declared
              ladder as the chosen model's provider takes it (lib/effort);
              nothing renders until the catalog has answered. No cost warning
              on Ultra by design — Codex's own TUI communicates quota impact,
              and Claude's equivalent carries none either. */}
          {offeredEfforts.length > 0 && (
            <div className="flex flex-col gap-2 sm:flex-row sm:items-center sm:justify-between sm:gap-4">
              <div>
                <p className="text-sm font-medium text-p-text">Default Effort</p>
                <p className="text-xs text-p-text-light">Thinking effort level</p>
              </div>
              <div className="flex items-center gap-2">
                <select
                  value={shownEffort || 'high'}
                  onChange={(e) => {
                    setDefaultEffort(e.target.value)
                    save('default_effort', e.target.value)
                  }}
                  className="px-2.5 py-1.5 text-sm border border-p-border-light rounded-lg bg-p-bg text-p-text focus:outline-hidden focus:ring-2 focus:ring-brand/30"
                >
                  {offeredEfforts.map(level => (
                    <option key={level} value={level}>{effortLabel(level)}</option>
                  ))}
                </select>
                <SavedIndicator show={savedField === 'default_effort'} />
              </div>
            </div>
          )}

          {/* Visibility & workspace — the four modes (collaborative × default
              scope). Replaces the old Default-Scope dropdown + Internal toggle;
              `default_scope` still drives tasks / notifications / triggers /
              meetings / memory. */}
          <div className="flex flex-col gap-3">
            <div className="flex items-start justify-between gap-4">
              <div>
                <p className="text-sm font-medium text-p-text">Visibility &amp; workspace</p>
                <p className="text-xs text-p-text-light">
                  Who shares this agent's files, chats, and memory — and the
                  default scope for its tasks, notifications, triggers, and meetings.
                </p>
              </div>
              <SavedIndicator show={savedField === 'visibility_mode'} />
            </div>

            {isReadOnly ? (
              <div className="rounded-lg border border-p-border-light bg-p-bg px-3 py-2">
                <p className="text-sm font-medium text-p-text">{MODE_LABEL[mode]}</p>
                <p className="text-xs text-p-text-light mt-0.5">{MODE_SUMMARY[mode]}</p>
              </div>
            ) : (
              <>
                <div className="space-y-3">
                  {MODE_GROUPS.map((group) => (
                    <fieldset key={group.label} className="space-y-1.5">
                      <legend className="text-xs font-semibold text-p-text-secondary mb-1">
                        {group.label}
                      </legend>
                      {group.modes.map((m) => {
                        const selected = mode === m
                        return (
                          <label
                            key={m}
                            className={`flex items-start gap-2.5 rounded-lg border px-3 py-2 cursor-pointer transition-colors ${
                              selected
                                ? 'border-brand bg-brand-surface'
                                : 'border-p-border-light hover:bg-p-surface-hover'
                            }`}
                          >
                            <input
                              type="radio"
                              name="visibility-mode"
                              checked={selected}
                              onChange={() => onSelectMode(m)}
                              className="mt-0.5 accent-brand"
                            />
                            <span className="min-w-0">
                              <span className="block text-sm font-medium text-p-text">{MODE_LABEL[m]}</span>
                              <span className="block text-xs text-p-text-light">{MODE_OPTION_HINT[m]}</span>
                            </span>
                          </label>
                        )
                      })}
                    </fieldset>
                  ))}
                </div>
                <p className="text-xs text-p-text-secondary">{MODE_SUMMARY[mode]}</p>
                {switchNotice && <p className="text-xs text-amber-600">{switchNotice}</p>}
              </>
            )}
          </div>

          {/* Admin Only — admin users only */}
          {isAdmin && (
            <div className="flex flex-col gap-2 sm:flex-row sm:items-center sm:justify-between sm:gap-4">
              <div>
                <p className="text-sm font-medium text-p-text">Admin Only</p>
                <p className="text-xs text-p-text-light">Restrict access to admin users</p>
              </div>
              <div className="flex items-center gap-2">
                <Toggle
                  checked={adminOnly}
                  onChange={(v) => {
                    setAdminOnly(v)
                    save('admin_only', v)
                  }}
                />
                <SavedIndicator show={savedField === 'admin_only'} />
              </div>
            </div>
          )}

          {/* Default for new users — admin only.
              Non-admin managers don't see this; flipping it affects every
              platform user (auto-attach at signup), so it's a platform-admin
              policy decision, not a per-agent-manager one. */}
          {isAdmin && (
            <div className="flex flex-col gap-2">
              <div className="flex flex-col gap-2 sm:flex-row sm:items-center sm:justify-between sm:gap-4">
                <div>
                  <p className="text-sm font-medium text-p-text">Default for new users</p>
                  <p className="text-xs text-p-text-light">
                    Every newly-created user is auto-attached to this agent with the chosen role.
                    Existing users are unaffected. Admin-only.
                  </p>
                </div>
                <div className="flex items-center gap-2">
                  <Toggle
                    checked={dfnuEnabled}
                    onChange={(v) => {
                      // A Shared-only agent takes editor or above: turning the
                      // default on there starts from editor, never a lower role.
                      const role = v && mode === 'shared_only' && !allowedOnSharedOnly(dfnuRole)
                        ? ROLE.EDITOR : dfnuRole
                      setDfnuEnabled(v)
                      setDfnuRole(role)
                      setDfnuError(null)
                      setDefaultForNewUsers.mutate(
                        { agent: name!, enabled: v, role: v ? role : null },
                        {
                          onSuccess: () => {
                            setSavedField('default_for_new_users_role')
                            setTimeout(() => setSavedField(null), 1500)
                          },
                          onError: (e) => {
                            setDfnuEnabled(!v)
                            setDfnuRole(dfnuRole)
                            setDfnuError(e.message)
                          },
                        },
                      )
                    }}
                  />
                  <SavedIndicator show={savedField === 'default_for_new_users_role'} />
                </div>
              </div>
              <div className={`flex flex-col gap-2 sm:flex-row sm:items-center sm:justify-between sm:gap-4 ${dfnuEnabled ? '' : 'opacity-40'}`}>
                <p className="text-xs text-p-text-secondary">Role assigned at auto-attach</p>
                <select
                  value={dfnuRole}
                  disabled={!dfnuEnabled}
                  onChange={(e) => {
                    const v = e.target.value as AgentRole
                    const previous = dfnuRole
                    setDfnuRole(v)
                    setDfnuError(null)
                    if (dfnuEnabled) {
                      setDefaultForNewUsers.mutate(
                        { agent: name!, enabled: true, role: v },
                        {
                          onSuccess: () => {
                            setSavedField('default_for_new_users_role')
                            setTimeout(() => setSavedField(null), 1500)
                          },
                          onError: (err) => {
                            setDfnuRole(previous)
                            setDfnuError(err.message)
                          },
                        },
                      )
                    }
                  }}
                  className="w-full sm:w-auto px-2.5 py-1.5 text-sm border border-p-border-light rounded-lg bg-p-bg text-p-text focus:outline-hidden focus:ring-2 focus:ring-brand/30 disabled:cursor-not-allowed"
                >
                  {/* A Shared-only agent takes editor or above; a lower default
                      stored before the switch shows, marked, and cannot be picked. */}
                  {(mode !== 'shared_only' || dfnuRole === ROLE.VIEWER) && (
                    <option value={ROLE.VIEWER} disabled={mode === 'shared_only'}>
                      {mode === 'shared_only'
                        ? 'Viewer (not for a Shared-only agent)'
                        : 'Viewer (read-only, recommended for personal assistants)'}
                    </option>
                  )}
                  {(mode !== 'shared_only' || dfnuRole === ROLE.CONTRIBUTOR) && (
                    <option value={ROLE.CONTRIBUTOR} disabled={mode === 'shared_only'}>
                      {mode === 'shared_only'
                        ? 'Contributor (not for a Shared-only agent)'
                        : 'Contributor (writes the shared workspace, nothing as the agent)'}
                    </option>
                  )}
                  <option value={ROLE.EDITOR}>Editor (shared workspace edits and agent-scope automations)</option>
                  <option value={ROLE.MANAGER}>Manager (full configuration access)</option>
                </select>
              </div>
              {dfnuError && <p className="text-xs text-red-600">{dfnuError}</p>}
            </div>
          )}
        </div>
      </div>

      {/* Memory — managers + admins. Rows gate on the mode's available scopes. */}
      <MemorySection name={name!} mode={mode} />


      {/* Delegation Targets */}
      {delegationData && delegationData.available.length > 0 && (
        <div className="bg-white dark:bg-p-surface rounded-xl border border-p-border-light p-4">
          <div className="flex items-center justify-between mb-4">
            <div>
              <p className="text-xs font-semibold text-p-text-secondary uppercase">Delegation Targets</p>
              <p className="text-xs text-p-text-light mt-0.5">Agents this agent can delegate tasks and send files to; a wired target's activity is also readable by this agent's autonomous runs (meetings follow each user's access instead)</p>
            </div>
            <SavedIndicator show={savedField === 'delegation_targets'} />
          </div>
          {/* Checkbox-style cards — same pattern as the AI Engines selection. */}
          <div className="grid grid-cols-1 sm:grid-cols-2 gap-2">
            {delegationData.available.map((agent) => {
              // Department-compiled edge → locked row: checked + disabled,
              // annotated with its source department. Never part of the PUT.
              const compiled = compiledByTarget.get(agent.name)
              const selected = selectedTargets.has(agent.name)
              if (compiled) {
                return (
                  <button
                    key={agent.name}
                    type="button"
                    disabled
                    title="Wired by department — managed automatically"
                    className="min-w-0 flex items-center gap-2.5 rounded-lg border px-3 py-2 text-left border-brand bg-brand-surface cursor-not-allowed opacity-60"
                  >
                    <span className="w-4 h-4 shrink-0 rounded-sm border flex items-center justify-center bg-brand border-brand text-white">
                      <svg className="w-3 h-3" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                        <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={3} d="M5 13l4 4L19 7" />
                      </svg>
                    </span>
                    <span
                      className="w-5 h-5 rounded-full shrink-0 flex items-center justify-center text-white text-[9px] font-bold"
                      style={{ backgroundColor: agent.color || '#6B7280' }}
                    >
                      {agent.name.charAt(0).toUpperCase()}
                    </span>
                    <span className="text-sm text-p-text truncate">{agent.display_name}</span>
                    <span className="text-xs text-p-text-light italic">via {compiled.department_name}</span>
                  </button>
                )
              }
              return (
                <button
                  key={agent.name}
                  type="button"
                  disabled={isReadOnly}
                  onClick={() => {
                    // Autosave per toggle (platform convention — same as the
                    // MCP toggles / policy radios): a selection that only
                    // LOOKS applied until a Save click gets silently lost.
                    const next = new Set(selectedTargets)
                    if (next.has(agent.name)) next.delete(agent.name)
                    else next.add(agent.name)
                    setSelectedTargets(next)
                    setDelegationTargets.mutate(
                      // Manual set only — compiled targets are filtered even
                      // if one ever leaks into selectedTargets (defensive;
                      // the sync effect above already strips them).
                      { agent: name!, targets: Array.from(next).filter((t) => !compiledByTarget.has(t)) },
                      {
                        onSuccess: () => {
                          setSavedField('delegation_targets')
                          setTimeout(() => setSavedField(null), 1500)
                        },
                        // On failure resync to the server's truth (manual only).
                        onError: () => setSelectedTargets(new Set(
                          delegationData.targets.filter((t) => !compiledByTarget.has(t)),
                        )),
                      },
                    )
                  }}
                  className={`min-w-0 flex items-center gap-2.5 rounded-lg border px-3 py-2 text-left transition-colors ${
                    selected ? 'border-brand bg-brand-surface' : 'border-p-border-light hover:bg-p-surface-hover'
                  } ${isReadOnly ? 'cursor-not-allowed opacity-60' : 'cursor-pointer'}`}
                >
                  <span className={`w-4 h-4 shrink-0 rounded-sm border flex items-center justify-center ${
                    selected ? 'bg-brand border-brand text-white' : 'border-p-border-light'
                  }`}>
                    {selected && (
                      <svg className="w-3 h-3" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                        <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={3} d="M5 13l4 4L19 7" />
                      </svg>
                    )}
                  </span>
                  <span
                    className="w-5 h-5 rounded-full shrink-0 flex items-center justify-center text-white text-[9px] font-bold"
                    style={{ backgroundColor: agent.color || '#6B7280' }}
                  >
                    {agent.name.charAt(0).toUpperCase()}
                  </span>
                  <span className="text-sm text-p-text truncate">{agent.display_name}</span>
                </button>
              )
            })}
          </div>
        </div>
      )}

      {/* Shared knowledge — promote this agent's knowledge folder as an
          installation-wide library and/or attach other agents' libraries.
          Wiring is platform-role territory (admins + creators; backend 403s
          everyone else); per-agent managers see the state read-only. The GET
          is manager-tier, so the card is hidden-not-rejected below manager. */}
      {knowledgeData && (
        <div className="bg-white dark:bg-p-surface rounded-xl border border-p-border-light p-4">
          <div className="flex items-center justify-between mb-4">
            <div>
              <p className="text-xs font-semibold text-p-text-secondary uppercase">Shared Knowledge</p>
              <p className="text-xs text-p-text-light mt-0.5">
                Libraries mirror into <span className="font-mono">knowledge/shared/&lt;source&gt;/&lt;folder&gt;/</span> on
                attached agents. Read-only mirrors are edited on the source agent. A library's{' '}
                <span className="font-mono">bulletin/&lt;name&gt;.md</span> is injected into every attached agent's context.
              </p>
            </div>
            <SavedIndicator show={savedField === 'shared_knowledge'} />
          </div>
          <div className="divide-y divide-p-border-light [&>*]:py-4 [&>*:first-child]:pt-0 [&>*:last-child]:pb-0">
            {/* Source half — libraries this agent shares (whole folder or
                disjoint knowledge subfolders, each independent). */}
            <div className="flex flex-col gap-2">
              <div className="flex flex-col gap-2 sm:flex-row sm:items-center sm:justify-between sm:gap-4">
                <div>
                  <p className="text-sm font-medium text-p-text">Shared libraries</p>
                  <p className="text-xs text-p-text-light">Share this agent's knowledge folder — or subfolders — for other agents to attach</p>
                </div>
                {canMutateKnowledge && !shareFormOpen && (
                  <button
                    type="button"
                    onClick={() => {
                      setLibraryNameDraft('')
                      setLibrarySubdirDraft('')
                      setRenamingSubdir(null)
                      setShareError('')
                      setShareFormOpen(true)
                    }}
                    className="px-3 py-1.5 text-xs rounded-lg border border-p-border-light text-p-text hover:bg-p-surface-hover transition-colors self-start sm:self-auto"
                  >
                    Share a folder…
                  </button>
                )}
              </div>
              {knowledgeData.libraries.length === 0 && !shareFormOpen && (
                <p className="text-xs text-p-text-light">Nothing shared yet.</p>
              )}
              {knowledgeData.libraries.map((lib) => (
                <div key={lib.subdir} className="flex items-center gap-2 rounded-lg border border-p-border-light px-3 py-2">
                  <span className="flex-1 min-w-0 truncate">
                    <span className="text-sm text-p-text">{lib.name || name}</span>
                    <span className="text-xs font-mono text-p-text-light"> · {lib.subdir ? `${lib.subdir}/` : 'whole folder'}</span>
                    {lib.has_bulletin && (
                      <span
                        className="ml-1.5 text-[10px] font-semibold px-1.5 py-0.5 rounded-sm bg-brand/10 text-brand border border-brand/30"
                        title={`bulletin/${lib.name}.md is injected into every attached agent's context`}
                      >
                        bulletin
                      </span>
                    )}
                  </span>
                  {lib.consumers.length > 0 ? (
                    <span className="hidden sm:flex flex-wrap gap-1.5">
                      {lib.consumers.map((c) => (
                        <span
                          key={c.consumer_agent}
                          className="text-[10px] font-semibold px-1.5 py-0.5 rounded-sm bg-p-bg text-p-text-secondary border border-p-border-light"
                        >
                          {c.consumer_agent} ({c.writable ? 'RW' : 'RO'})
                        </span>
                      ))}
                    </span>
                  ) : (
                    <span className="hidden sm:inline text-xs text-p-text-light">no consumers</span>
                  )}
                  {canMutateKnowledge && (
                    <>
                      <button
                        type="button"
                        aria-label={`Rename library ${lib.subdir || 'root'}`}
                        title="Rename this library (its bulletin file follows the name)"
                        onClick={() => {
                          setLibraryNameDraft(lib.name)
                          setLibrarySubdirDraft(lib.subdir)
                          setRenamingSubdir(lib.subdir)
                          setShareError('')
                          setShareFormOpen(true)
                        }}
                        className="shrink-0 p-1 rounded-sm text-p-text-light hover:text-p-text hover:bg-p-surface-hover transition-colors cursor-pointer"
                      >
                        <svg className="w-3.5 h-3.5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                          <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M11 5H6a2 2 0 00-2 2v11a2 2 0 002 2h11a2 2 0 002-2v-5m-1.414-9.414a2 2 0 112.828 2.828L11.828 15H9v-2.828l8.586-8.586z" />
                        </svg>
                      </button>
                      <button
                        type="button"
                        aria-label={`Unshare library ${lib.subdir || 'root'}`}
                        title="Stop sharing this library"
                        onClick={() => {
                          if (lib.consumers.length > 0) setUnshareTarget(lib)
                          else unshareLibrary(lib.subdir)
                        }}
                        className="shrink-0 p-1 rounded-sm text-p-text-light hover:text-red-600 hover:bg-red-50 dark:hover:bg-red-900/20 transition-colors cursor-pointer"
                      >
                        <svg className="w-3.5 h-3.5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                          <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M6 18L18 6M6 6l12 12" />
                        </svg>
                      </button>
                    </>
                  )}
                </div>
              ))}
              {shareFormOpen && (
                <div className="flex flex-col gap-2">
                  {renamingSubdir !== null ? (
                    // Rename keeps the (source, subdir) identity — show the
                    // locked folder instead of the picker.
                    <div className="px-3 py-1.5 text-sm font-mono rounded-lg border border-p-border-light bg-p-surface text-p-text-secondary">
                      {renamingSubdir ? `knowledge/${renamingSubdir}/` : 'knowledge/ (whole folder)'}
                    </div>
                  ) : (
                    // Folder picker — the agent's knowledge tree, replacing
                    // the old free-text subfolder field (operator ask
                    // 2026-08-24: browse and pick, like the workspace list).
                    <div
                      role="listbox"
                      aria-label="Library subfolder"
                      className="max-h-44 overflow-y-auto rounded-lg border border-p-border-light divide-y divide-p-border-light/60"
                    >
                      {[{ path: '', depth: 0 }, ...knowledgeFolders].map(({ path, depth }) => {
                        const overlap = shareOverlap(path)
                        const selected = librarySubdirDraft === path
                        return (
                          <button
                            key={path || '(root)'}
                            type="button"
                            role="option"
                            aria-selected={selected}
                            disabled={!!overlap}
                            onClick={() => setLibrarySubdirDraft(path)}
                            title={overlap ? `Overlaps the shared library “${overlap}”` : undefined}
                            style={{ paddingLeft: `${0.75 + depth * 1.1}rem` }}
                            className={`w-full flex items-center gap-2 py-1.5 pr-3 text-sm text-left transition-colors ${
                              selected ? 'bg-brand/10 text-brand' : 'text-p-text hover:bg-p-surface-hover'
                            } disabled:opacity-45 disabled:cursor-not-allowed cursor-pointer`}
                          >
                            <svg className="w-3.5 h-3.5 shrink-0" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                              <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2}
                                    d="M3 7v10a2 2 0 002 2h14a2 2 0 002-2V9a2 2 0 00-2-2h-6l-2-2H5a2 2 0 00-2 2z" />
                            </svg>
                            <span className={path ? 'font-mono' : ''}>
                              {path || 'Whole knowledge folder'}
                            </span>
                            {overlap && (
                              // aria-hidden: the option's accessible name
                              // stays the bare path (the title carries why).
                              <span aria-hidden="true" className="ml-auto text-[10px] text-p-text-light shrink-0">shared</span>
                            )}
                          </button>
                        )
                      })}
                    </div>
                  )}
                  <div className="flex flex-col gap-2 sm:flex-row sm:items-center">
                    <input
                      autoFocus
                      value={libraryNameDraft}
                      maxLength={64}
                      onChange={(e) => setLibraryNameDraft(e.target.value)}
                      onKeyDown={(e) => {
                        if (e.key === 'Enter' && libraryNameDraft.trim()
                            && (renamingSubdir !== null || !shareOverlap(librarySubdirDraft))) {
                          shareLibrary(librarySubdirDraft.trim().replace(/^\/+|\/+$/g, ''), libraryNameDraft.trim())
                        } else if (e.key === 'Escape') {
                          setShareFormOpen(false)
                          setRenamingSubdir(null)
                          setShareError('')
                        }
                      }}
                      placeholder="Name this library, e.g. Brand Guidelines"
                      aria-label="Library name"
                      className="flex-1 px-3 py-1.5 text-sm rounded-lg border border-p-border-light bg-p-bg text-p-text placeholder:text-p-text-light focus:outline-none focus:ring-2 focus:ring-brand/40"
                    />
                    <div className="flex gap-2">
                      <button
                        onClick={() => shareLibrary(librarySubdirDraft.trim().replace(/^\/+|\/+$/g, ''), libraryNameDraft.trim())}
                        disabled={!libraryNameDraft.trim()
                          || (renamingSubdir === null && shareOverlap(librarySubdirDraft) !== null)}
                        className="px-3 py-1.5 text-xs rounded-lg bg-brand text-white hover:bg-brand/90 transition-colors disabled:opacity-50"
                      >
                        {renamingSubdir !== null ? 'Rename' : 'Share'}
                      </button>
                      <button
                        onClick={() => {
                          setShareFormOpen(false)
                          setRenamingSubdir(null)
                          setShareError('')
                        }}
                        className="px-3 py-1.5 text-xs rounded-lg border border-p-border-light text-p-text-secondary hover:bg-p-surface-hover transition-colors"
                      >
                        Cancel
                      </button>
                    </div>
                  </div>
                  {shareError && (
                    <p className="text-xs text-red-600 dark:text-red-400">{shareError}</p>
                  )}
                </div>
              )}
            </div>

            {/* Consumer half — libraries attached to this agent */}
            <div className="flex flex-col gap-2">
              <div>
                <p className="text-sm font-medium text-p-text">Attached libraries</p>
                <p className="text-xs text-p-text-light">Shared knowledge this agent can read/write</p>
              </div>
              {knowledgeData.attachments.length === 0 && (
                <p className="text-xs text-p-text-light">No libraries attached.</p>
              )}
              {knowledgeData.attachments.map((a) => {
                const key = libKey(a.source_agent, a.subdir)
                const writable = attachmentWritable[key] ?? a.writable
                return (
                  <div key={key} className="flex items-center gap-2 rounded-lg border border-p-border-light px-3 py-2">
                    {/* Label first, mount path second — libraries shared
                        before names existed fall back to the agent slug. */}
                    <span className="flex-1 min-w-0 truncate">
                      <span className="text-sm text-p-text">{a.name || a.source_agent}</span>
                      <span className="text-xs font-mono text-p-text-light"> · {libMountPath(a.source_agent, a.subdir)}</span>
                      {a.has_bulletin && (
                        <span
                          className="ml-1.5 text-[10px] font-semibold px-1.5 py-0.5 rounded-sm bg-brand/10 text-brand border border-brand/30"
                          title="This library publishes a bulletin into this agent's context"
                        >
                          bulletin
                        </span>
                      )}
                    </span>
                    {canMutateKnowledge ? (
                      <>
                        <button
                          type="button"
                          aria-label={`Writable ${a.source_agent}${a.subdir ? ` ${a.subdir}` : ''}`}
                          title={writable
                            ? 'Writable — this agent edits the shared library directly. Click for read-only.'
                            : 'Read-only — the library is edited on its source agent. Click to make writable.'}
                          onClick={() => {
                            const next = !writable
                            setAttachmentWritable({ ...attachmentWritable, [key]: next })
                            attachKnowledgeLibrary.mutate(
                              { agent: name!, source_agent: a.source_agent, subdir: a.subdir, writable: next },
                              {
                                onSuccess: () => {
                                  setSavedField('shared_knowledge')
                                  setTimeout(() => setSavedField(null), 1500)
                                },
                                // On failure resync to the server's truth.
                                onError: () => setAttachmentWritable(Object.fromEntries(
                                  knowledgeData.attachments.map((x) => [libKey(x.source_agent, x.subdir), x.writable]),
                                )),
                              },
                            )
                          }}
                          className={`shrink-0 px-1.5 py-0.5 rounded-sm text-[10px] font-semibold border transition-colors cursor-pointer ${
                            writable
                              ? 'border-brand bg-brand text-white'
                              : 'border-p-border-light text-p-text-secondary hover:bg-p-surface-hover'
                          }`}
                        >
                          {writable ? 'RW' : 'RO'}
                        </button>
                        <button
                          type="button"
                          aria-label={`Detach ${a.source_agent}${a.subdir ? ` ${a.subdir}` : ''}`}
                          title="Detach this library and remove its mirror"
                          onClick={() => detachKnowledgeLibrary.mutate(
                            { agent: name!, source: a.source_agent, subdir: a.subdir },
                            {
                              onSuccess: () => {
                                setSavedField('shared_knowledge')
                                setTimeout(() => setSavedField(null), 1500)
                              },
                            },
                          )}
                          className="shrink-0 p-1 rounded-sm text-p-text-light hover:text-red-600 hover:bg-red-50 dark:hover:bg-red-900/20 transition-colors cursor-pointer"
                        >
                          <svg className="w-3.5 h-3.5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                            <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M6 18L18 6M6 6l12 12" />
                          </svg>
                        </button>
                      </>
                    ) : (
                      <span className="shrink-0 px-1.5 py-0.5 rounded-sm text-[10px] font-semibold bg-p-bg text-p-text-secondary border border-p-border-light">
                        {writable ? 'RW' : 'RO'}
                      </span>
                    )}
                  </div>
                )
              })}
              {canMutateKnowledge && attachableLibraries.length > 0 && (
                <div className="flex flex-wrap items-center gap-2">
                  {/* Mobile: full-width + min-w-0 so long library labels
                      ellipsize inside the card instead of widening it. */}
                  <select
                    aria-label="Attach library"
                    value={attachPick}
                    onChange={(e) => setAttachPick(e.target.value)}
                    className="w-full min-w-0 sm:w-auto sm:max-w-xs px-2.5 py-1.5 text-sm border border-p-border-light rounded-lg bg-p-bg text-p-text focus:outline-hidden focus:ring-2 focus:ring-brand/30"
                  >
                    <option value="">Attach library…</option>
                    {attachableLibraries.map((l) => (
                      <option key={libKey(l.source_agent, l.subdir)} value={JSON.stringify([l.source_agent, l.subdir])}>
                        {(l.name ? `${l.name} · ` : '') + l.source_agent + (l.subdir ? `/${l.subdir}` : '')}
                      </option>
                    ))}
                  </select>
                  <label className="flex items-center gap-1.5 text-xs text-p-text-secondary cursor-pointer">
                    <input
                      type="checkbox"
                      checked={attachWritable}
                      onChange={(e) => setAttachWritable(e.target.checked)}
                      className="accent-brand"
                    />
                    Writable
                  </label>
                  <button
                    type="button"
                    disabled={!attachPick}
                    onClick={() => {
                      const [pickSource, pickSubdir] = JSON.parse(attachPick) as [string, string]
                      attachKnowledgeLibrary.mutate(
                        { agent: name!, source_agent: pickSource, subdir: pickSubdir, writable: attachWritable },
                        {
                          onSuccess: () => {
                            setAttachPick('')
                            setAttachWritable(false)
                            setSavedField('shared_knowledge')
                            setTimeout(() => setSavedField(null), 1500)
                          },
                        },
                      )
                    }}
                    className="px-3 py-1.5 text-sm font-medium rounded-lg border border-p-border-light text-p-text hover:bg-p-surface-hover transition-colors disabled:opacity-50 disabled:cursor-not-allowed"
                  >
                    Attach
                  </button>
                </div>
              )}
            </div>
          </div>
        </div>
      )}

      {/* Danger Zone — admin only */}
      {isAdmin && (
        <div className="rounded-xl border-2 border-red-300 dark:border-red-800 p-4">
          <p className="text-xs font-semibold text-red-600 uppercase mb-2">Danger Zone</p>
          <div className="flex items-center justify-between">
            <div>
              <p className="text-sm font-medium text-p-text">Delete this agent</p>
              <p className="text-xs text-p-text-light">Once deleted, this cannot be undone.</p>
            </div>
            <button
              onClick={() => setDeleteModalOpen(true)}
              className="px-3 py-1.5 text-sm font-medium rounded-lg border border-red-300 dark:border-red-700 text-red-600 hover:bg-red-50 dark:hover:bg-red-900/20 transition-colors"
            >
              Delete Agent
            </button>
          </div>
        </div>
      )}

      {deleteModalOpen && (
        <DeleteModal
          slug={name!}
          onConfirm={handleDelete}
          onCancel={() => setDeleteModalOpen(false)}
          isPending={deleteAgent.isPending}
        />
      )}

      {/* Un-promote confirmation — only when consumers are attached: the
          server detaches them all and tears their mirror subtrees down
          (sibling libraries are untouched; v1 warns, never blocks). */}
      {unshareTarget && (
        <StrongConfirmModal
          title={`Stop sharing "${unshareTarget.name || name}"?`}
          description={
            <>
              Un-sharing this library
              {unshareTarget.subdir ? <> (<span className="font-mono">{unshareTarget.subdir}/</span>)</> : null}
              {' '}detaches every attached agent and removes their mirrors:{' '}
              <strong>
                {unshareTarget.consumers.map((c) => c.consumer_agent).join(', ')}
              </strong>
              . Their sessions lose the shared folder at next start.
            </>
          }
          confirmWord="CONFIRM"
          confirmLabel="Stop sharing"
          onCancel={() => setUnshareTarget(null)}
          onConfirm={() => {
            unshareLibrary(unshareTarget.subdir)
            setUnshareTarget(null)
          }}
        />
      )}

      {/* Shared-only flip confirmation. Existing chats are never deleted; only
          which chats a user sees changes (one shared list ↔ per-user lists). */}
      {pendingMode && (
        <StrongConfirmModal
          title={`Switch to ${MODE_LABEL[pendingMode]}?`}
          description={
            pendingMode === 'shared_only' ? (
              <>
                In <strong>Shared only</strong>, everyone assigned to this agent shares
                one workspace and <strong>one chat history</strong> — each person will
                see everybody's conversations. Existing per-user chats are kept but stop
                appearing in the chat list; new chats are shared.
              </>
            ) : (
              <>
                Leaving <strong>Shared only</strong> gives each person their own chats
                again. The existing shared conversations are kept but stop appearing in
                the chat list; new chats are per-user.
              </>
            )
          }
          extra={pendingMode === 'shared_only' && (losing.length > 0 || switchError || listPending || usersFailed) ? (
            <div className="text-sm text-p-text">
              {listPending && <p className="text-xs text-p-text-light">Loading who loses access…</p>}
              {usersFailed && !switchPeople && (
                <p className="text-xs text-p-text-light">
                  The list of assignments could not be read: confirming asks the server for it.
                </p>
              )}
              {losing.length > 0 && (
                <>
                  <p className="mb-1">
                    Shared only takes the <strong>editor</strong> role or above to chat, so
                    {' '}{losing.length === 1 ? 'this person loses' : `these ${losing.length} people lose`}
                    {' '}their assignment to this agent:
                  </p>
                  <ul className="max-h-40 overflow-y-auto rounded-lg border border-p-border-light px-3 py-1.5 text-xs">
                    {losing.map((p) => (
                      <li key={p.sub} className="flex justify-between gap-2 py-0.5">
                        <span className="truncate">{p.name}</span>
                        <span className="shrink-0 text-p-text-light">{roleLabel(p.role)}</span>
                      </li>
                    ))}
                  </ul>
                </>
              )}
              {switchError && <p className="mt-2 text-xs text-red-600">{switchError}</p>}
            </div>
          ) : undefined}
          confirmWord="CONFIRM"
          confirmLabel="Switch mode"
          destructive={pendingMode === 'shared_only' && losing.length > 0}
          isPending={switchMode.isPending}
          confirmDisabled={listPending}
          onCancel={() => { setPendingMode(null); setSwitchPeople(null); setSwitchError(null) }}
          onConfirm={() => {
            if (pendingMode === 'shared_only') {
              confirmSharedOnly()
              return
            }
            saveMode(pendingMode)
            setPendingMode(null)
          }}
        />
      )}
    </div>
  )
}
