import { Fragment, useState, useEffect, useCallback, useLayoutEffect, useMemo, useRef } from 'react'
import { useParams, useNavigate } from 'react-router-dom'
import { useQueryClient } from '@tanstack/react-query'
import { useAgentInfo, useUpdateAgent, useDeleteAgent, useDelegationTargets, useSetDelegationTargets, useExecutionLayers, useSetDefaultForNewUsers, useKnowledgeAttachments, useKnowledgeLibraries, useSetKnowledgeLibrary, useAttachKnowledgeLibrary, useDetachKnowledgeLibrary, useAgentFiles, useAgentUsers, AgentUpdateError, type AgentInfo, type AgentUser, type SharedOnlySwitchResult, type KnowledgeLibrary, type FileNode } from '../../api/agents'
import { LOCAL_LABEL, placePhrase, useRemoteMachines } from '../../api/remoteMachines'
import { PAIRING_SCOPE, TARGET_LOCAL, isLocalTarget } from '../../lib/placement'
import { MACHINE_STATE, machineStateWord } from '../../lib/status/machine'
import { useDepartments } from '../../api/departments'
import { wiringSentence } from '../../lib/kinds/department'
import { useAuth } from '../../contexts/AuthContext'
import { useFlag } from '../../hooks/useFlag'
import { ROLE, allowedOnSharedOnly, canManageAgent, isAdmin as isPlatformAdmin, isCreatorOrAbove, roleLabel, type AgentRole } from '../../lib/permissions'
import InvokedForm from '../../components/common/InvokedForm'
import { tierMarkText, tierTitle } from '../../lib/tiers'
import { toastError } from '../../lib/http/apiJson'
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
import Switch from '../../components/ui/Switch'
import Button from '../../components/ui/Button'
import { Input, Select } from '../../components/ui/Field'
import InfoTip from '../../components/ui/InfoTip'
import { SaveState, SettingRow } from '../../components/ui/SettingsControls'
import { useSaveState } from '../../hooks/useSaveState'
import { BADGE_BASE, BADGE_TONE, CARD, CARD_BORDERED, FIELD, FIELD_BORDER, FIELD_REGULAR } from '../../components/ui/classes'
import { MemorySection } from './AgentConfig.memory'
import { HEAD } from '../../lib/layout/tree'
import { COLOR_PRESETS } from './AgentConfig.presets'

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
  const remoteMachinesAvailable = useFlag('remote_machines_available')
  const interactiveTerminalEnabled = useFlag('interactive_terminal_enabled')
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
  const [adminOnly, setAdminOnly] = useState(false)
  const [showTemplateUpdate, setShowTemplateUpdate] = useState(false)
  const [agentColor, setAgentColor] = useState('')
  const [displayName, setDisplayName] = useState('')
  const [description, setDescription] = useState('')
  const { stateOf, run, markSaved } = useSaveState()
  const [deleteModalOpen, setDeleteModalOpen] = useState(false)
  const [executionTarget, setExecutionTarget] = useState<string>(TARGET_LOCAL)
  const targetMachine = (machines ?? []).find(m => m.id === executionTarget)
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
  const shareButtonRef = useRef<HTMLButtonElement>(null)
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

  // The agent's row fills the form when it arrives or changes (in render).
  const [infoShown, setInfoShown] = useState<typeof info>(undefined)
  if (info && infoShown !== info) {
    setInfoShown(info)
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
  }

  const [knowledgeShown, setKnowledgeShown] = useState<typeof knowledgeData>(undefined)
  if (knowledgeData && knowledgeShown !== knowledgeData) {
    setKnowledgeShown(knowledgeData)
    setAttachmentWritable(Object.fromEntries(
      knowledgeData.attachments.map(
        (a) => [libKey(a.source_agent, a.subdir), a.writable]),
    ))
  }

  const [delegationShown, setDelegationShown] = useState<typeof delegationData>(undefined)
  if (delegationData && delegationShown !== delegationData) {
    setDelegationShown(delegationData)
    // selectedTargets holds MANUAL targets only — it's the PUT payload.
    // Department-compiled targets are locked rows the compiler owns; if an
    // agent somehow appears in both lists (defensive), compiled wins so a
    // toggle round-trip can never re-submit it as manual.
    const locked = new Set((delegationData.compiled ?? []).map((e) => e.target))
    setSelectedTargets(new Set(delegationData.targets.filter((t) => !locked.has(t))))
  }

  // A revert puts back the agent's row as the server has it when the refusal
  // lands, never the value from before the click: two quick changes may both
  // be refused.
  const stored = useRef(info)
  useLayoutEffect(() => { stored.current = info })
  const back = useCallback((apply: (i: AgentInfo) => void) => () => {
    if (stored.current) apply(stored.current)
  }, [])
  const revertDfnu = back((i) => {
    const role = i.default_for_new_users_role
    setDfnuEnabled(!!role)
    setDfnuRole(role || (isSharedOnly(modeOfAgent(i)) ? ROLE.EDITOR : ROLE.VIEWER))
  })

  // `mutateAsync` and `run` keep their identity across renders, so `save`
  // does too and the effects that list it never fire twice. `revert` puts
  // the person's own change back; a correction the page makes by itself
  // passes none (a revert would bring the stale value back and the check
  // would fire again).
  const { mutateAsync: mutateAgentAsync } = updateAgent
  const save = useCallback(
    (field: string, value: unknown, revert?: () => void) => {
      void run(field, () => mutateAgentAsync({ name: name!, [field]: value }),
        { failTitle: 'The setting was not saved', revert })
    },
    [name, mutateAgentAsync, run],
  )

  // Current visibility mode + a one-PATCH writer that persists both columns
  // together (so the agent never lands in a transient half-applied state).
  const mode = modeOf(collaborative, defaultScope)
  const saveMode = useCallback(
    (next: VisibilityMode) => {
      const cols = columnsOf(next)
      setCollaborative(cols.collaborative)
      setDefaultScope(cols.default_scope)
      void run('visibility_mode', () => updateAgent.mutateAsync(
        { name: name!, collaborative: cols.collaborative, default_scope: cols.default_scope },
      ), {
        failTitle: 'The visibility mode was not saved',
        revert: back((i) => { setCollaborative(i.collaborative ?? true); setDefaultScope(i.default_scope || 'user') }),
      })
    },
    [name, updateAgent, run, back],
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
      void run('department', () => updateAgent.mutateAsync(
        { name: name!, department_id: deptId, department_level_id: levelId },
      ).then(() => Promise.all([
        queryClient.invalidateQueries({ queryKey: ['departments'] }),
        queryClient.invalidateQueries({ queryKey: ['delegation-targets', name] }),
      ])), {
        failTitle: 'The department was not saved',
        revert: back((i) => { setDepartmentId(i.department_id || ''); setDepartmentLevelId(i.department_level_id || '') }),
      })
    },
    [name, updateAgent, queryClient, run, back],
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
  // loads (in render; a pick that is free stays, so this converges).
  if (shareFormOpen && renamingSubdir === null && shareOverlap(librarySubdirDraft)) {
    const free = ['', ...knowledgeFolders.map((f) => f.path)]
      .find((p) => !shareOverlap(p)) ?? ''
    if (free !== librarySubdirDraft) setLibrarySubdirDraft(free)
  }

  // Share (or rename — same PUT) one library: a subtree of this agent's
  // knowledge folder ('' = the whole folder). Server-side validation
  // (subdir shape, overlap, bulletin rename conflicts) lands in shareError.
  // `close` is the open form's own close, which gives the focus back.
  const shareLibrary = useCallback(
    (subdir: string, libraryName: string, close: (setClosed: () => void) => void) => {
      setShareError('')
      void run(`knowledge:own:${subdir}`, () => setKnowledgeLibrary.mutateAsync(
        { agent: name!, enabled: true, name: libraryName, subdir },
      ).then(() => {
        close(() => {
          setShareFormOpen(false)
          setRenamingSubdir(null)
        })
        setLibraryNameDraft('')
        setLibrarySubdirDraft('')
      }), {
        onFail: (e) => setShareError(e instanceof Error ? e.message : 'Failed to share library'),
      })
    },
    [name, setKnowledgeLibrary, run],
  )

  // Un-share ONE library. With consumers attached the caller routes through
  // a type-to-confirm first (unshareTarget) — the server detaches them all
  // and tears their mirror subtrees down (v1 warns, never blocks).
  const unshareLibrary = useCallback(
    (subdir: string) => {
      void run(`knowledge:own:${subdir}`, () => setKnowledgeLibrary.mutateAsync(
        { agent: name!, enabled: false, subdir },
      ), { failTitle: 'The library was not unshared' })
    },
    [name, setKnowledgeLibrary, run],
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
            notes.push(`${r.not_removed.length} viewer or contributor assignment(s) were kept. `
              + 'They were added during the switch, or their removal failed. An admin can change them in Admin, Users.')
          }
          if (r?.default_cleared) notes.push('The role new users are attached with was cleared.')
          setSwitchNotice(notes.length ? notes.join(' ') : null)
          queryClient.invalidateQueries({ queryKey: ['agent-users', name] })
          queryClient.invalidateQueries({ queryKey: ['admin-users'] })
          if (user?.sub && r?.removed?.includes(user.sub)) void refreshUser()
          markSaved('visibility_mode')
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
  // The row is corrected in render; each correction is saved once, for the
  // agent it was read from: the page stays mounted across a route from one
  // agent's config to another's, and a correction never lands on the next.
  const [execModeResets, setExecModeResets] = useState({ agent: name, n: 0 })
  if (catalogLoaded && canManage && defaultExecutionMode && defaultModel && !defaultModelIsCliLayer) {
    setDefaultExecutionMode('')
    setExecModeResets((w) => ({ agent: name, n: w.n + 1 }))
  }
  useEffect(() => {
    if (execModeResets.n > 0 && execModeResets.agent === name) save('default_execution_mode', '')
  }, [execModeResets, save, name])

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
  const [effortWrites, setEffortWrites] = useState({ agent: name, n: 0, value: '' })
  if (catalogLoaded && canManage && defaultEffort && shownEffort !== defaultEffort) {
    setDefaultEffort(shownEffort)
    setEffortWrites((w) => ({ agent: name, n: w.n + 1, value: shownEffort }))
  }
  useEffect(() => {
    if (effortWrites.n > 0 && effortWrites.agent === name) save('default_effort', effortWrites.value)
  }, [effortWrites, save, name])

  const handleDelete = () => {
    deleteAgent.mutateAsync({ name: name!, confirm_slug: name! })
      .then(() => navigate('/agents'))
      .catch(toastError('The agent was not deleted'))
  }

  if (isLoading) return <p className="text-sm text-secondary">Loading...</p>

  const agentRole = name ? user?.agent_roles?.[name] : undefined
  const isReadOnly = !canManage  // editor + viewer: read-only

  // Department-compiled delegation edges, keyed by target. Rendered as
  // locked (checked + disabled) rows and NEVER included in the PUT payload —
  // the department compiler owns those edges server-side.
  const compiledByTarget = new Map(
    (delegationData?.compiled ?? []).map((e) => [e.target, e] as const),
  )
  // The selected department's own wiring, so the row says what the locked
  // "via" rows below will contain.
  const assignedDept = (departments ?? []).find((d) => d.id === departmentId)

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

  // The share form opens where it was invoked: under the header for a new
  // share, under the library's row for a rename.
  const closeShareForm = () => {
    setShareFormOpen(false)
    setRenamingSubdir(null)
    setShareError('')
  }
  const shareForm = shareFormOpen && (
    <InvokedForm onCancel={closeShareForm} fallback={shareButtonRef} className="flex flex-col gap-2">
      {(close) => (
        <>
          {renamingSubdir !== null ? (
            // Rename keeps the (source, subdir) identity — show the
            // locked folder instead of the picker.
            <div className="px-3 py-1.5 text-sm font-mono rounded-control bg-surface-muted text-secondary">
              {renamingSubdir ? `knowledge/${renamingSubdir}/` : 'The whole knowledge/ folder'}
            </div>
          ) : (
            // Folder picker: the agent's knowledge tree, browsed and picked
            // like the workspace list.
            <div
              role="listbox"
              aria-label="Library subfolder"
              className="max-h-44 overflow-y-auto rounded-card border border-subtle divide-y divide-subtle"
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
                      selected ? 'bg-action-subtle text-link' : 'text-primary hover:bg-surface-hover'
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
                      <span aria-hidden="true" className="ml-auto text-3xs text-tertiary shrink-0">shared</span>
                    )}
                  </button>
                )
              })}
            </div>
          )}
          <div className="flex flex-col gap-2 sm:flex-row sm:items-center">
            <Input
              value={libraryNameDraft}
              maxLength={64}
              onChange={(e) => setLibraryNameDraft(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === 'Enter' && libraryNameDraft.trim()
                    && (renamingSubdir !== null || !shareOverlap(librarySubdirDraft))) {
                  shareLibrary(librarySubdirDraft.trim().replace(/^\/+|\/+$/g, ''), libraryNameDraft.trim(), close)
                }
              }}
              placeholder="Name this library, e.g. Brand Guidelines"
              aria-label="Library name"
              className="flex-1"
            />
            <div className="flex gap-2">
              <Button
                variant="primary" size="xs"
                onClick={() => shareLibrary(librarySubdirDraft.trim().replace(/^\/+|\/+$/g, ''), libraryNameDraft.trim(), close)}
                disabled={!libraryNameDraft.trim()
                  || (renamingSubdir === null && shareOverlap(librarySubdirDraft) !== null)}
              >
                {renamingSubdir !== null ? 'Rename' : 'Share'}
              </Button>
              <Button size="xs" onClick={() => close(closeShareForm)}>
                Cancel
              </Button>
            </div>
          </div>
          {shareError && (
            <p className="text-xs text-status-danger-fg">{shareError}</p>
          )}
        </>
      )}
    </InvokedForm>
  )

  return (
    <div className="space-y-6">
      {isReadOnly && (
        <div className="bg-status-warning-bg border border-status-warning-border rounded-card px-4 py-3 text-xs text-status-warning-fg">
          <strong>Read-only.</strong> Agent settings are owner-only.{' '}
          {agentRole === ROLE.EDITOR
            ? 'As an editor you can work on the agent\'s shared workspace and files. An owner sets how the agent behaves: its prompt, MCPs, knowledge and default scope.'
            : agentRole === ROLE.CONTRIBUTOR
              ? 'As a contributor you can add and edit files in the agent\'s shared workspace. An owner sets how the agent behaves: its prompt, MCPs, knowledge and default scope.'
            : agentRole === ROLE.VIEWER
              ? 'As a viewer you can read the agent\'s workspace, knowledge and settings. Only owners can change them.'
              : 'You do not have manager access to this agent.'}
        </div>
      )}
      {canManage && info?.template_update?.update_available && (
        <div
          className="bg-status-info-bg border border-status-info-border rounded-card px-4 py-3 text-xs text-status-info-fg flex flex-wrap items-center justify-between gap-2"
          data-testid="template-update-banner"
        >
          <span>
            <strong>Version {info.template_update.catalog_version}</strong> of this agent's template is available
            {info.template_update.installed_version ? `, and you have ${info.template_update.installed_version}` : ''}.
            {!info.template_update.compat_ok ? ' It needs a newer OtoDock.' : ' What you changed stays. The update shows what it replaces before it runs.'}
          </span>
          <Button
            variant="primary" size="xs"
            onClick={() => setShowTemplateUpdate(true)}
            disabled={!info.template_update.compat_ok}
          >
            Update
          </Button>
        </div>
      )}
      {showTemplateUpdate && name && (
        <AgentUpdateModal open agentSlug={name} onClose={() => setShowTemplateUpdate(false)} />
      )}
      {/* Settings */}
      <div className={`${CARD} p-4`}>
        <p className="text-xs font-semibold text-secondary uppercase mb-4">Agent Configuration</p>
        {/* divide-y draws a subtle separator between every setting; the child
            padding utilities give each row consistent breathing room. */}
        <div className="divide-y divide-subtle [&>*]:py-4 [&>*:first-child]:pt-0 [&>*:last-child]:pb-0">
          {/* Display Name */}
          <div className="flex flex-col gap-2 sm:flex-row sm:items-center sm:justify-between sm:gap-4">
            <div>
              <p className="text-sm font-medium text-primary">
                Display Name
                <InfoTip label="Display Name">Human-readable agent name</InfoTip>
                <SaveState state={stateOf('display_name')} />
              </p>
            </div>
            <div className="flex items-center gap-2">
              <Input
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
                className="max-w-48 sm:w-48"
              />
            </div>
          </div>

          {/* Slug (read-only) */}
          <div className="flex flex-col gap-2 sm:flex-row sm:items-center sm:justify-between sm:gap-4">
            <div>
              <p className="text-sm font-medium text-primary">
                Slug
                <InfoTip label="Slug">Unique identifier, read-only</InfoTip>
              </p>
            </div>
            <span className="px-2.5 py-1.5 text-sm text-secondary font-mono bg-surface-muted rounded-control">
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
                <p className="text-sm font-medium text-primary">
                  Template
                  <InfoTip label="Template">
                    The community template this agent came from. A newer version shows above when the community list has one.
                  </InfoTip>
                </p>
              </div>
              <span className="px-2.5 py-1.5 text-sm text-secondary font-mono bg-surface-muted rounded-control">
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
                <p className="text-sm font-medium text-primary">
                  Description
                  <InfoTip label="Description">What this agent does, shown on cards and to other agents</InfoTip>
                  <SaveState state={stateOf('description')} />
                </p>
              </div>
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
              className={`${FIELD} ${FIELD_BORDER} ${FIELD_REGULAR} resize-none`}
            />
          </div>

          {/* Agent Color */}
          <div className="flex flex-col gap-2 sm:flex-row sm:items-center sm:justify-between sm:gap-4">
            <div>
              <p className="text-sm font-medium text-primary">
                Agent Color
                <InfoTip label="Agent Color">Color for cards and chat avatar</InfoTip>
                <SaveState state={stateOf('color')} />
              </p>
            </div>
            <div className="flex items-center gap-2">
              <div className="flex gap-1.5 flex-wrap justify-end">
                {COLOR_PRESETS.map(({ hex, name }) => (
                  <button
                    key={hex}
                    title={name}
                    onClick={() => {
                      setAgentColor(hex)
                      save('color', hex, back((i) => setAgentColor(i.color || '')))
                    }}
                    className={`w-6 h-6 rounded-full border-2 transition-all ${
                      agentColor === hex
                        ? 'border-action scale-110'
                        : 'border-transparent hover:scale-105'
                    }`}
                    style={{ backgroundColor: hex }}
                  />
                ))}
              </div>
            </div>
          </div>

          {/* Execution Path */}
          <div className="flex flex-col gap-3">
            <div className="flex items-center justify-between gap-3">
              <div>
                <p className="text-sm font-medium text-primary">
                  AI Engines
                  <InfoTip label="AI Engines">Which AI engines this agent can use</InfoTip>
                  <SaveState state={stateOf('execution_paths')} />
                </p>
              </div>
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
                      save('execution_paths', next, back((i) => {
                        setExecutionPaths(i.execution_paths || (i.execution_path ? [i.execution_path] : []))
                        setExecutionPath(i.execution_path || '')
                      }))
                    }}
                    className={`w-full flex items-start gap-2.5 rounded-control border px-3 py-2 text-left transition-colors ${
                      checked ? 'border-subtle bg-action-subtle' : 'border-subtle hover:bg-surface-hover'
                    } ${isReadOnly || lockedOff ? 'cursor-not-allowed opacity-60' : 'cursor-pointer'}`}
                  >
                    <span className={`mt-0.5 w-4 h-4 shrink-0 rounded-sm border flex items-center justify-center ${
                      checked ? 'bg-action border-action text-action-fg' : 'border-strong'
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
                          <span className="text-3xs font-semibold uppercase tracking-wide px-1.5 py-0.5 rounded-sm bg-surface-muted text-secondary">
                            {badge}
                          </span>
                        )}
                        <span className="text-sm font-medium text-primary">{label}</span>
                      </span>
                      {!isCoding(engine) && (
                        <span className="block text-xs text-tertiary mt-0.5">
                          Supporting engine: fewer tools, faster replies.
                        </span>
                      )}
                      {!configured && !checked && (
                        <span className="block text-xs text-status-warning-fg mt-0.5">
                          No {label} subscription is connected on this platform
                          {isAdmin ? '. Connect one in Setup, AI Engines.' : '.'}
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
          {isAdmin && remoteMachinesAvailable && (
          <div className="flex flex-col gap-2 sm:flex-row sm:items-center sm:justify-between sm:gap-4">
            <div>
              <p className="text-sm font-medium text-primary">
                Execution Target
                {runsRemote(primaryEngine) && (
                  <InfoTip label="Execution Target">
                    {`Where this agent runs: ${placePhrase(TARGET_LOCAL, '')} or a remote machine`}
                  </InfoTip>
                )}
                <SaveState state={stateOf('execution_target')} />
              </p>
              {!runsRemote(primaryEngine) && (
                <p className="text-xs text-tertiary">
                  {`This engine always runs on ${placePhrase(TARGET_LOCAL, '')}`}
                </p>
              )}
            </div>
            {/* Mobile: the select takes the full row (min-w-0 lets it shrink
                below its widest option) and the status badge wraps BELOW it;
                from sm: up the badge sits beside an auto-width select. */}
            <div className="flex flex-wrap items-center gap-2 min-w-0">
              {!runsRemote(primaryEngine) ? (
                <span className="text-sm text-tertiary">{LOCAL_LABEL}</span>
              ) : (
                <Select
                  value={executionTarget}
                  onChange={e => {
                    setExecutionTarget(e.target.value)
                    save('execution_target', e.target.value, back((i) => setExecutionTarget(i.execution_target || TARGET_LOCAL)))
                  }}
                  className="sm:w-auto sm:max-w-xs"
                >
                  <option value={TARGET_LOCAL}>{LOCAL_LABEL}</option>
                  {(machines ?? []).filter(m => m.pairing_scope === PAIRING_SCOPE.ADMIN).map(m => (
                    <option key={m.id} value={m.id}>
                      {m.status === MACHINE_STATE.ONLINE ? m.name : `${m.name}, ${machineStateWord(m.status)}`}
                    </option>
                  ))}
                </Select>
              )}
              {!isLocalTarget(executionTarget) && (
                <span className={`${BADGE_BASE} ${
                  targetMachine?.status === MACHINE_STATE.ONLINE
                    ? BADGE_TONE.success
                    : BADGE_TONE.warning
                }`}>
                  {targetMachine ? machineStateWord(targetMachine.status) : 'unknown'}
                </span>
              )}
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
              <p className="text-sm font-medium text-primary">
                Department
                {!assignedDept && (
                  <InfoTip label="Department">
                    Auto-wires delegation within the department, following its delegation mode and reach.
                  </InfoTip>
                )}
                <SaveState state={stateOf('department')} />
              </p>
              {assignedDept && (
                <p className="text-xs text-tertiary">{wiringSentence(assignedDept.mode, assignedDept.reach)}</p>
              )}
            </div>
            <div className="flex items-center gap-2">
              <Select
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
                className="max-w-max sm:w-auto"
              >
                <option value="">None</option>
                {(departments ?? []).map((d) => (
                  <option key={d.id} value={d.id}>{d.name}</option>
                ))}
              </Select>
              {departmentId && (
                <Select
                  aria-label="Level"
                  value={departmentLevelId}
                  disabled={isReadOnly}
                  onChange={(e) => saveDepartment(departmentId, e.target.value)}
                  className="max-w-max sm:w-auto"
                >
                  {[...((departments ?? []).find((d) => d.id === departmentId)?.levels ?? [])]
                    .sort((a, b) => a.rank - b.rank)
                    .map((l) => (
                      <option key={l.id} value={l.id}>{l.name}</option>
                    ))}
                </Select>
              )}
            </div>
          </div>
          )}

          {/* Default Model */}
          <div className="flex flex-col gap-2 sm:flex-row sm:items-center sm:justify-between sm:gap-4">
            <div>
              <p className="text-sm font-medium text-primary">
                Default Model
                <InfoTip label="Default Model">Model used when no override is set</InfoTip>
                <SaveState state={stateOf('default_model')} />
              </p>
            </div>
            <div className="flex items-center gap-2">
              <Select
                value={defaultModel}
                onChange={(e) => {
                  setDefaultModel(e.target.value)
                  save('default_model', e.target.value, back((i) => setDefaultModel(i.default_model || '')))
                }}
                className="sm:w-auto sm:max-w-xs"
              >
                <option value="">{autoModel ? `Auto: ${autoModel.label}` : 'Auto'}</option>
                {modelGroups.map(g => (
                  <optgroup key={g.path} label={g.label}>
                    {g.models.map(m => (
                      <option key={m.value} value={m.value} title={m.tier || m.good_at ? tierTitle(m.tier, m.tier_label, m.good_at) : undefined}>
                        {m.label}{m.tier ? `  ${tierMarkText(m.tier)}` : ''}
                      </option>
                    ))}
                  </optgroup>
                ))}
              </Select>
            </div>
          </div>

          {/* Default Session Mode (Interactive CLI) — only when the
              default model runs on an engine with a native TUI (its
              descriptor's supports_interactive_pty), and only when the
              platform-wide interactive kill-switch is on. Manager-gated
              server-side (PATCH /v1/agents requires manage). */}
          {defaultModelIsCliLayer && interactiveTerminalEnabled && (
            <div className="flex flex-col gap-2 sm:flex-row sm:items-center sm:justify-between sm:gap-4">
              <div>
                <p className="text-sm font-medium text-primary">
                  Default Session Mode
                  <InfoTip label="Default Session Mode">
                    How new chats and tasks start: the normal way, or in the
                    Interactive terminal
                  </InfoTip>
                  <SaveState state={stateOf('default_execution_mode')} />
                </p>
              </div>
              <div className="flex items-center gap-2">
                <Select
                  value={defaultExecutionMode || '-p'}
                  onChange={(e) => {
                    const v = e.target.value as 'interactive' | '-p'
                    setDefaultExecutionMode(v)
                    save('default_execution_mode', v, back((i) => setDefaultExecutionMode(i.default_execution_mode || '')))
                  }}
                  className="max-w-max sm:w-auto"
                >
                  <option value="-p">Normal, without a terminal</option>
                  <option value="interactive">Interactive terminal</option>
                </Select>
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
                <p className="text-sm font-medium text-primary">
                  Default Effort
                  <InfoTip label="Default Effort">Thinking effort level</InfoTip>
                  <SaveState state={stateOf('default_effort')} />
                </p>
              </div>
              <div className="flex items-center gap-2">
                <Select
                  value={shownEffort || 'high'}
                  onChange={(e) => {
                    setDefaultEffort(e.target.value)
                    save('default_effort', e.target.value, back((i) => setDefaultEffort(i.default_effort || '')))
                  }}
                  className="max-w-max sm:w-auto"
                >
                  {offeredEfforts.map(level => (
                    <option key={level} value={level}>{effortLabel(level)}</option>
                  ))}
                </Select>
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
                <p className="text-sm font-medium text-primary">
                  Visibility and workspace
                  <InfoTip label="Visibility and workspace">
                    Who shares this agent's files, chats and memory. It also sets the
                    default scope for its tasks, notifications, triggers and meetings.
                  </InfoTip>
                  <SaveState state={stateOf('visibility_mode')} />
                </p>
              </div>
            </div>

            {isReadOnly ? (
              <div className={`${CARD_BORDERED} px-3 py-2`}>
                <p className="text-sm font-medium text-primary">{MODE_LABEL[mode]}</p>
                <p className="text-xs text-tertiary mt-0.5">{MODE_SUMMARY[mode]}</p>
              </div>
            ) : (
              <>
                <div className="space-y-3">
                  {MODE_GROUPS.map((group) => (
                    <fieldset key={group.label} className="space-y-1.5">
                      <legend className="text-xs font-semibold text-secondary mb-1">
                        {group.label}
                      </legend>
                      {group.modes.map((m) => {
                        const selected = mode === m
                        return (
                          <label
                            key={m}
                            className={`flex items-start gap-2.5 rounded-control border px-3 py-2 cursor-pointer transition-colors ${
                              selected
                                ? 'border-subtle bg-action-subtle'
                                : 'border-subtle hover:bg-surface-hover'
                            }`}
                          >
                            <input
                              type="radio"
                              name="visibility-mode"
                              checked={selected}
                              onChange={() => onSelectMode(m)}
                              className="mt-0.5 accent-action"
                            />
                            <span className="min-w-0">
                              <span className="block text-sm font-medium text-primary">{MODE_LABEL[m]}</span>
                              <span className="block text-xs text-tertiary">{MODE_OPTION_HINT[m]}</span>
                            </span>
                          </label>
                        )
                      })}
                    </fieldset>
                  ))}
                </div>
                <p className="text-xs text-secondary">{MODE_SUMMARY[mode]}</p>
                {switchNotice && <p className="text-xs text-status-warning-fg">{switchNotice}</p>}
              </>
            )}
          </div>

          {/* Admin Only — admin users only */}
          {isAdmin && (
            <SettingRow
              label="Admin Only"
              info="Restrict access to admin users"
              save={stateOf('admin_only')}
            >
              <Switch
                aria-label="Admin only"
                checked={adminOnly}
                onChange={(v) => {
                  setAdminOnly(v)
                  save('admin_only', v, back((i) => setAdminOnly(i.admin_only ?? false)))
                }}
              />
            </SettingRow>
          )}

          {/* Default for new users — admin only.
              Non-admin managers don't see this; flipping it affects every
              platform user (auto-attach at signup), so it's a platform-admin
              policy decision, not a per-agent-manager one. */}
          {isAdmin && (
            <div className="flex flex-col gap-2">
              <SettingRow
                label="Default for new users"
                description="Every newly-created user is auto-attached to this agent with the chosen role."
                info="Existing users are unaffected. Admin-only."
                save={stateOf('default_for_new_users_role')}
              >
                <Switch
                  aria-label="Default for new users"
                  checked={dfnuEnabled}
                  onChange={(v) => {
                    // A Shared-only agent takes editor or above: turning the
                    // default on there starts from editor, never a lower role.
                    const role = v && mode === 'shared_only' && !allowedOnSharedOnly(dfnuRole)
                      ? ROLE.EDITOR : dfnuRole
                    setDfnuEnabled(v)
                    setDfnuRole(role)
                    void run('default_for_new_users_role', () => setDefaultForNewUsers.mutateAsync(
                      { agent: name!, enabled: v, role: v ? role : null },
                    ), {
                      failTitle: 'The default for new users was not saved',
                      revert: revertDfnu,
                    })
                  }}
                />
              </SettingRow>
              <div className={`flex flex-col gap-2 sm:flex-row sm:items-center sm:justify-between sm:gap-4 ${dfnuEnabled ? '' : 'opacity-40'}`}>
                <p className="text-xs text-secondary">Role assigned at auto-attach</p>
                <Select
                  value={dfnuRole}
                  disabled={!dfnuEnabled}
                  onChange={(e) => {
                    const v = e.target.value as AgentRole
                    setDfnuRole(v)
                    if (dfnuEnabled) {
                      void run('default_for_new_users_role', () => setDefaultForNewUsers.mutateAsync(
                        { agent: name!, enabled: true, role: v },
                      ), {
                        failTitle: 'The default for new users was not saved',
                        revert: revertDfnu,
                      })
                    }
                  }}
                  className="sm:w-auto"
                >
                  {/* A Shared-only agent takes editor or above; a lower default
                      stored before the switch shows, marked, and cannot be picked. */}
                  {(mode !== 'shared_only' || dfnuRole === ROLE.VIEWER) && (
                    <option value={ROLE.VIEWER} disabled={mode === 'shared_only'}>
                      {mode === 'shared_only'
                        ? 'Viewer: not for a Shared-only agent'
                        : 'Viewer: read-only, recommended for personal assistants'}
                    </option>
                  )}
                  {(mode !== 'shared_only' || dfnuRole === ROLE.CONTRIBUTOR) && (
                    <option value={ROLE.CONTRIBUTOR} disabled={mode === 'shared_only'}>
                      {mode === 'shared_only'
                        ? 'Contributor: not for a Shared-only agent'
                        : 'Contributor: writes the shared workspace, nothing as the agent'}
                    </option>
                  )}
                  <option value={ROLE.EDITOR}>Editor: shared workspace edits and agent-scope automations</option>
                  <option value={ROLE.MANAGER}>Manager: full configuration access</option>
                </Select>
              </div>
            </div>
          )}
        </div>
      </div>

      {/* Memory — managers + admins. Rows gate on the mode's available scopes. */}
      <MemorySection name={name!} mode={mode} />


      {/* Delegation Targets */}
      {delegationData && delegationData.available.length > 0 && (
        <div className={`${CARD} p-4`}>
          <div className="flex items-center justify-between mb-4">
            <div>
              <p className="text-xs font-semibold text-secondary uppercase">
                Delegation Targets
                <InfoTip label="Delegation Targets">
                  Agents this agent can delegate tasks and send files to. This agent's autonomous runs can also read a target's activity. Meetings follow each user's access instead.
                </InfoTip>
                <span className="normal-case tracking-normal"><SaveState state={stateOf('delegation_targets')} /></span>
              </p>
            </div>
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
                    title="Wired by the department, managed automatically"
                    className="min-w-0 flex items-center gap-2.5 rounded-control border px-3 py-2 text-left border-subtle bg-action-subtle cursor-not-allowed opacity-60"
                  >
                    <span className="w-4 h-4 shrink-0 rounded-sm border flex items-center justify-center bg-action border-action text-action-fg">
                      <svg className="w-3 h-3" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                        <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={3} d="M5 13l4 4L19 7" />
                      </svg>
                    </span>
                    <span
                      className="w-5 h-5 rounded-full shrink-0 flex items-center justify-center text-on-solid text-3xs font-bold"
                      style={{ backgroundColor: agent.color || 'var(--color-accent-agent-fill)' }}
                    >
                      {agent.name.charAt(0).toUpperCase()}
                    </span>
                    <span className="text-sm text-primary truncate">{agent.display_name}</span>
                    <span className="text-xs text-tertiary italic">via {compiled.department_name}</span>
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
                    void run('delegation_targets', () => setDelegationTargets.mutateAsync(
                      // Manual set only — compiled targets are filtered even
                      // if one ever leaks into selectedTargets (defensive;
                      // the sync effect above already strips them).
                      { agent: name!, targets: Array.from(next).filter((t) => !compiledByTarget.has(t)) },
                    ), {
                      failTitle: 'The delegation targets were not saved',
                      // Back to the server's truth (manual only).
                      revert: () => setSelectedTargets(new Set(
                        delegationData.targets.filter((t) => !compiledByTarget.has(t)),
                      )),
                    })
                  }}
                  className={`min-w-0 flex items-center gap-2.5 rounded-control border px-3 py-2 text-left transition-colors ${
                    selected ? 'border-subtle bg-action-subtle' : 'border-subtle hover:bg-surface-hover'
                  } ${isReadOnly ? 'cursor-not-allowed opacity-60' : 'cursor-pointer'}`}
                >
                  <span className={`w-4 h-4 shrink-0 rounded-sm border flex items-center justify-center ${
                    selected ? 'bg-action border-action text-action-fg' : 'border-strong'
                  }`}>
                    {selected && (
                      <svg className="w-3 h-3" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                        <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={3} d="M5 13l4 4L19 7" />
                      </svg>
                    )}
                  </span>
                  <span
                    className="w-5 h-5 rounded-full shrink-0 flex items-center justify-center text-on-solid text-3xs font-bold"
                    style={{ backgroundColor: agent.color || 'var(--color-accent-agent-fill)' }}
                  >
                    {agent.name.charAt(0).toUpperCase()}
                  </span>
                  <span className="text-sm text-primary truncate">{agent.display_name}</span>
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
        <div className={`${CARD} p-4`}>
          <div className="flex items-center justify-between mb-4">
            <div>
              <p className="text-xs font-semibold text-secondary uppercase">
                Shared Knowledge
                <span className="normal-case tracking-normal"><SaveState state={stateOf('knowledge')} /></span>
              </p>
              <p className="text-xs text-tertiary mt-0.5">
                Libraries mirror into <span className="font-mono">knowledge/shared/&lt;source&gt;/&lt;folder&gt;/</span> on
                attached agents. Read-only mirrors are edited on the source agent. A library's{' '}
                <span className="font-mono">bulletin/&lt;name&gt;.md</span> is injected into every attached agent's context.
              </p>
            </div>
          </div>
          <div className="divide-y divide-subtle [&>*]:py-4 [&>*:first-child]:pt-0 [&>*:last-child]:pb-0">
            {/* Source half — libraries this agent shares (whole folder or
                disjoint knowledge subfolders, each independent). */}
            <div className="flex flex-col gap-2">
              <div className="flex flex-col gap-2 sm:flex-row sm:items-center sm:justify-between sm:gap-4">
                <div>
                  <p className="text-sm font-medium text-primary">
                    Shared libraries
                    <InfoTip label="Shared libraries">
                      Share this agent's knowledge folder, or some of its subfolders, for other agents to attach
                    </InfoTip>
                  </p>
                </div>
                {canMutateKnowledge && !shareFormOpen && (
                  <Button
                    ref={shareButtonRef} size="xs" className="self-start sm:self-auto"
                    onClick={() => {
                      setLibraryNameDraft('')
                      setLibrarySubdirDraft('')
                      setRenamingSubdir(null)
                      setShareError('')
                      setShareFormOpen(true)
                    }}
                  >
                    Share a folder…
                  </Button>
                )}
              </div>
              {knowledgeData.libraries.length === 0 && !shareFormOpen && (
                <p className="text-xs text-tertiary">Nothing shared yet.</p>
              )}
              {renamingSubdir === null && shareForm}
              {knowledgeData.libraries.map((lib) => (
                <Fragment key={lib.subdir}>
                  <div className="flex items-center gap-2 rounded-card border border-subtle px-3 py-2">
                    <span className="flex-1 min-w-0 truncate">
                      <span className="text-sm text-primary">{lib.name || name}</span>
                      <span className="text-xs font-mono text-tertiary"> · {lib.subdir ? `${lib.subdir}/` : 'whole folder'}</span>
                      {lib.has_bulletin && (
                        <span
                          className={`ml-1.5 text-3xs font-semibold px-1.5 py-0.5 rounded-sm ${BADGE_TONE.info}`}
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
                            className="text-3xs font-semibold px-1.5 py-0.5 rounded-sm bg-surface-muted text-secondary"
                          >
                            {c.consumer_agent} ({c.writable ? 'RW' : 'RO'})
                          </span>
                        ))}
                      </span>
                    ) : (
                      <span className="hidden sm:inline text-xs text-tertiary">No consumers</span>
                    )}
                    {canMutateKnowledge && (
                      <>
                        <button
                          type="button"
                          aria-label={`Rename library ${lib.subdir || 'root'}`}
                          title="Rename this library. Its bulletin file follows the name."
                          onClick={() => {
                            setLibraryNameDraft(lib.name)
                            setLibrarySubdirDraft(lib.subdir)
                            setRenamingSubdir(lib.subdir)
                            setShareError('')
                            setShareFormOpen(true)
                          }}
                          className="shrink-0 p-1 rounded-sm text-tertiary hover:text-primary hover:bg-surface-hover transition-colors cursor-pointer"
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
                          className="shrink-0 p-1 rounded-sm text-tertiary hover:text-status-danger-fg hover:bg-status-danger-bg transition-colors cursor-pointer"
                        >
                          <svg className="w-3.5 h-3.5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                            <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M6 18L18 6M6 6l12 12" />
                          </svg>
                        </button>
                      </>
                    )}
                  </div>
                  {renamingSubdir === lib.subdir && shareForm}
                </Fragment>
              ))}
            </div>

            {/* Consumer half — libraries attached to this agent */}
            <div className="flex flex-col gap-2">
              <div>
                <p className="text-sm font-medium text-primary">
                  Attached libraries
                  <InfoTip label="Attached libraries">Shared knowledge this agent can read or write</InfoTip>
                </p>
              </div>
              {knowledgeData.attachments.length === 0 && (
                <p className="text-xs text-tertiary">No libraries attached.</p>
              )}
              {knowledgeData.attachments.map((a) => {
                const key = libKey(a.source_agent, a.subdir)
                const writable = attachmentWritable[key] ?? a.writable
                return (
                  <div key={key} className="flex items-center gap-2 rounded-card border border-subtle px-3 py-2">
                    {/* Label first, mount path second — libraries shared
                        before names existed fall back to the agent slug. */}
                    <span className="flex-1 min-w-0 truncate">
                      <span className="text-sm text-primary">{a.name || a.source_agent}</span>
                      <span className="text-xs font-mono text-tertiary"> · {libMountPath(a.source_agent, a.subdir)}</span>
                      {a.has_bulletin && (
                        <span
                          className={`ml-1.5 text-3xs font-semibold px-1.5 py-0.5 rounded-sm ${BADGE_TONE.info}`}
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
                            ? 'Writable. This agent edits the shared library directly. Click for read-only.'
                            : 'Read-only. The library is edited on its source agent. Click to make writable.'}
                          onClick={() => {
                            const next = !writable
                            setAttachmentWritable({ ...attachmentWritable, [key]: next })
                            void run(`knowledge:${key}`, () => attachKnowledgeLibrary.mutateAsync(
                              { agent: name!, source_agent: a.source_agent, subdir: a.subdir, writable: next },
                            ), {
                              failTitle: 'The library access was not changed',
                              // Back to the server's truth.
                              revert: () => setAttachmentWritable(Object.fromEntries(
                                knowledgeData.attachments.map((x) => [libKey(x.source_agent, x.subdir), x.writable]),
                              )),
                            })
                          }}
                          className={`shrink-0 px-1.5 py-0.5 rounded-sm text-3xs font-semibold border transition-colors cursor-pointer ${
                            writable
                              ? 'border-action bg-action text-action-fg'
                              : 'border-default text-secondary hover:bg-surface-hover'
                          }`}
                        >
                          {writable ? 'RW' : 'RO'}
                        </button>
                        <button
                          type="button"
                          aria-label={`Detach ${a.source_agent}${a.subdir ? ` ${a.subdir}` : ''}`}
                          title="Detach this library and remove its mirror"
                          onClick={() => {
                            void run(`knowledge:${key}`, () => detachKnowledgeLibrary.mutateAsync(
                              { agent: name!, source: a.source_agent, subdir: a.subdir },
                            ), { failTitle: 'The library was not detached' })
                          }}
                          className="shrink-0 p-1 rounded-sm text-tertiary hover:text-status-danger-fg hover:bg-status-danger-bg transition-colors cursor-pointer"
                        >
                          <svg className="w-3.5 h-3.5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                            <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M6 18L18 6M6 6l12 12" />
                          </svg>
                        </button>
                      </>
                    ) : (
                      <span className="shrink-0 px-1.5 py-0.5 rounded-sm text-3xs font-semibold bg-surface-muted text-secondary">
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
                  <Select
                    aria-label="Attach library"
                    value={attachPick}
                    onChange={(e) => setAttachPick(e.target.value)}
                    className="sm:w-auto sm:max-w-xs"
                  >
                    <option value="">Attach library…</option>
                    {attachableLibraries.map((l) => (
                      <option key={libKey(l.source_agent, l.subdir)} value={JSON.stringify([l.source_agent, l.subdir])}>
                        {(l.name ? `${l.name} · ` : '') + l.source_agent + (l.subdir ? `/${l.subdir}` : '')}
                      </option>
                    ))}
                  </Select>
                  <label className="flex items-center gap-1.5 text-xs text-secondary cursor-pointer">
                    <input
                      type="checkbox"
                      checked={attachWritable}
                      onChange={(e) => setAttachWritable(e.target.checked)}
                      className="accent-action"
                    />
                    Writable
                  </label>
                  <Button
                    disabled={!attachPick}
                    onClick={() => {
                      const [pickSource, pickSubdir] = JSON.parse(attachPick) as [string, string]
                      void run(`knowledge:${libKey(pickSource, pickSubdir)}`, () => attachKnowledgeLibrary.mutateAsync(
                        { agent: name!, source_agent: pickSource, subdir: pickSubdir, writable: attachWritable },
                      ).then(() => {
                        setAttachPick('')
                        setAttachWritable(false)
                      }), { failTitle: 'The library was not attached' })
                    }}
                  >
                    Attach
                  </Button>
                </div>
              )}
            </div>
          </div>
        </div>
      )}

      {/* Danger Zone — admin only */}
      {isAdmin && (
        <div className={`${CARD} border-2 border-status-danger-border p-4`}>
          <p className="text-xs font-semibold text-status-danger-fg uppercase mb-2">Danger Zone</p>
          <div className="flex items-center justify-between">
            <div>
              <p className="text-sm font-medium text-primary">Delete this agent</p>
              <p className="text-xs text-tertiary">Once deleted, this cannot be undone.</p>
            </div>
            <Button variant="danger" onClick={() => setDeleteModalOpen(true)}>
              Delete Agent
            </Button>
          </div>
        </div>
      )}

      {deleteModalOpen && (
        <StrongConfirmModal
          title="Delete Agent"
          description="This will permanently delete the agent and all its data."
          confirmWord={name!}
          confirmLabel="Delete Agent"
          busyLabel="Deleting..."
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
                one workspace and <strong>one chat history</strong>. Each person will
                see everybody's conversations. Existing per-user chats are kept but stop
                appearing in the chat list. New chats are shared.
              </>
            ) : (
              <>
                Leaving <strong>Shared only</strong> gives each person their own chats
                again. The existing shared conversations are kept but stop appearing in
                the chat list. New chats are per-user.
              </>
            )
          }
          extra={pendingMode === 'shared_only' && (losing.length > 0 || switchError || listPending || usersFailed) ? (
            <div className="text-sm text-primary">
              {listPending && <p className="text-xs text-tertiary">Loading who loses access…</p>}
              {usersFailed && !switchPeople && (
                <p className="text-xs text-tertiary">
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
                  <ul className="max-h-40 overflow-y-auto rounded-card border border-subtle px-3 py-1.5 text-xs">
                    {losing.map((p) => (
                      <li key={p.sub} className="flex justify-between gap-2 py-0.5">
                        <span className="truncate">{p.name}</span>
                        <span className="shrink-0 text-tertiary">{roleLabel(p.role)}</span>
                      </li>
                    ))}
                  </ul>
                </>
              )}
              {switchError && <p className="mt-2 text-xs text-status-danger-fg">{switchError}</p>}
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
