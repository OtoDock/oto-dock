/**
 * Departments editor — the classic (form-based) editor for the installation's
 * org structure, rendered as a tab on the agents page. The parent handles
 * routing/gating; this renders for any user:
 *   - CREATE is role-gated (admin/creator only — the create card is hidden
 *     for members),
 *   - EDIT is per-department via the server-computed `can_edit` flag.
 *
 * Level edits are structural (dropping a level drops its agents out of the
 * department), so the levels list is a local draft with an explicit
 * "Save levels" button — never autosaved per keystroke — and removals of
 * levels that still have members go through an inline confirm step.
 */
import { useEffect, useMemo, useState } from 'react'
import {
  useDepartments,
  useCreateDepartment,
  useUpdateDepartment,
  useDeleteDepartment,
  useSetDepartmentLevels,
  type Department,
  type DepartmentMember,
} from '../../api/departments'
import { useAuth } from '../../contexts/AuthContext'
import { Toggle } from '../../pages/agent/AgentConfig.parts'

const MAX_LEVELS = 8

const REACH_HINT: Record<'adjacent' | 'subtree', string> = {
  adjacent: 'Agents can automatically delegate work within their own level and one level up or down.',
  subtree: 'Every agent in the department can automatically delegate work to every other.',
}

// ---------------------------------------------------------------------------
// Shared class strings (AgentConfig card idiom)
// ---------------------------------------------------------------------------

const CARD = 'bg-white dark:bg-p-surface rounded-xl border border-p-border-light p-4'
const SECTION_LABEL = 'text-xs font-semibold uppercase tracking-wider text-p-text-light mb-3'
const INPUT =
  'px-2.5 py-1.5 text-sm border border-p-border-light rounded-lg bg-p-bg text-p-text focus:outline-hidden focus:ring-2 focus:ring-brand/30'
const PRIMARY_BTN =
  'bg-brand text-white rounded-lg px-3 py-1.5 text-sm hover:bg-brand-hover transition-colors disabled:opacity-40 disabled:cursor-not-allowed'
const SECONDARY_BTN =
  'px-3 py-1.5 text-sm rounded-lg border border-p-border-light text-p-text-secondary hover:bg-p-surface-hover transition-colors disabled:opacity-40 disabled:cursor-not-allowed'
const ICON_BTN =
  'px-1.5 py-1 text-xs leading-none rounded-md border border-p-border-light text-p-text-secondary hover:bg-p-surface-hover transition-colors disabled:opacity-30 disabled:cursor-not-allowed'
const AMBER_BOX =
  'bg-amber-50 dark:bg-amber-900/20 border border-amber-200 dark:border-amber-700 rounded-lg px-3 py-2 text-xs text-amber-800 dark:text-amber-200'
const ERROR_TEXT = 'text-xs text-red-600 dark:text-red-400'

// ---------------------------------------------------------------------------
// Small helpers
// ---------------------------------------------------------------------------

function move<T>(arr: T[], i: number, delta: number): T[] {
  const j = i + delta
  if (j < 0 || j >= arr.length) return arr
  const next = [...arr]
  ;[next[i], next[j]] = [next[j], next[i]]
  return next
}

function sortedLevels(d: Department) {
  return [...d.levels].sort((a, b) => a.rank - b.rank)
}

/** The shared "agents fell out of the department" warning line. */
function UnassignedWarning({ agents }: { agents: string[] }) {
  return (
    <div className={AMBER_BOX}>
      <strong>{agents.join(', ')}</strong> dropped out of the department — reassign them in
      Agent Settings.
    </div>
  )
}

// ---------------------------------------------------------------------------
// Create card (admin/creator only)
// ---------------------------------------------------------------------------

function CreateDepartmentCard({ onDone }: { onDone: () => void }) {
  const create = useCreateDepartment()
  const [name, setName] = useState('')
  const [levels, setLevels] = useState<string[]>(['Head', 'Senior', 'Junior'])
  const [reach, setReach] = useState<'adjacent' | 'subtree'>('adjacent')
  const [autoDelegation, setAutoDelegation] = useState(true)

  const valid = !!name.trim() && levels.length >= 1 && levels.every(l => l.trim())

  const onCreate = () => {
    if (!valid) return
    create.mutate(
      {
        name: name.trim(),
        auto_delegation: autoDelegation,
        reach,
        levels: levels.map(l => l.trim()),
      },
      {
        onSuccess: () => {
          setName('')
          setLevels(['Head', 'Senior', 'Junior'])
          setReach('adjacent')
          setAutoDelegation(true)
          onDone()
        },
      },
    )
  }

  return (
    <div className={CARD}>
      <p className={SECTION_LABEL}>New department</p>
      <div className="divide-y divide-p-border-light [&>*]:py-3 [&>*:first-child]:pt-0 [&>*:last-child]:pb-0">
        {/* Name */}
        <div className="flex flex-col gap-2 sm:flex-row sm:items-center sm:justify-between sm:gap-4">
          <div>
            <p className="text-sm font-medium text-p-text">Name</p>
            <p className="text-xs text-p-text-light">What this department is called on the map</p>
          </div>
          <input
            type="text"
            value={name}
            onChange={e => setName(e.target.value)}
            placeholder="e.g., Engineering"
            aria-label="New department name"
            className={`${INPUT} w-full sm:w-56`}
          />
        </div>

        {/* Initial levels */}
        <div className="flex flex-col gap-2">
          <div>
            <p className="text-sm font-medium text-p-text">Initial levels</p>
            <p className="text-xs text-p-text-light">Top rank first — you can rework these later</p>
          </div>
          <div className="space-y-1.5">
            {levels.map((lvl, i) => (
              <div key={i} className="flex items-center gap-2">
                <span className="text-xs text-p-text-light w-4 text-right shrink-0">{i + 1}</span>
                <input
                  type="text"
                  value={lvl}
                  onChange={e =>
                    setLevels(ls => ls.map((l, idx) => (idx === i ? e.target.value : l)))
                  }
                  aria-label="Initial level name"
                  className={`${INPUT} flex-1 min-w-0`}
                />
                <button
                  type="button"
                  aria-label="Move initial level up"
                  disabled={i === 0}
                  onClick={() => setLevels(ls => move(ls, i, -1))}
                  className={ICON_BTN}
                >
                  ↑
                </button>
                <button
                  type="button"
                  aria-label="Move initial level down"
                  disabled={i === levels.length - 1}
                  onClick={() => setLevels(ls => move(ls, i, 1))}
                  className={ICON_BTN}
                >
                  ↓
                </button>
                <button
                  type="button"
                  aria-label="Remove initial level"
                  disabled={levels.length <= 1}
                  onClick={() => setLevels(ls => ls.filter((_, idx) => idx !== i))}
                  className={ICON_BTN}
                >
                  ×
                </button>
              </div>
            ))}
          </div>
          <div>
            <button
              type="button"
              disabled={levels.length >= MAX_LEVELS}
              onClick={() => setLevels(ls => [...ls, ''])}
              className={`${SECONDARY_BTN} text-xs px-2.5 py-1`}
            >
              + Add initial level
            </button>
          </div>
        </div>

        {/* Reach */}
        <div className="flex flex-col gap-2 sm:flex-row sm:items-center sm:justify-between sm:gap-4">
          <div>
            <p className="text-sm font-medium text-p-text">Reach</p>
            <p className="text-xs text-p-text-light">{REACH_HINT[reach]}</p>
          </div>
          <select
            value={reach}
            onChange={e => setReach(e.target.value as 'adjacent' | 'subtree')}
            aria-label="New department reach"
            className={`${INPUT} w-full sm:w-56`}
          >
            <option value="adjacent">Adjacent — one level up &amp; down</option>
            <option value="subtree">Subtree — the whole department</option>
          </select>
        </div>

        {/* Auto-delegation */}
        <div className="flex items-center justify-between gap-4">
          <div>
            <p className="text-sm font-medium text-p-text">Auto-delegation</p>
            <p className="text-xs text-p-text-light">
              Automatically enable agents to delegate work to other agents
              of the department
            </p>
          </div>
          <Toggle checked={autoDelegation} onChange={setAutoDelegation} />
        </div>

        {/* Create */}
        <div className="flex items-center justify-between gap-3">
          {create.error ? (
            <p className={ERROR_TEXT}>{create.error.message}</p>
          ) : (
            <span />
          )}
          <div className="flex items-center gap-2">
            <button type="button" onClick={onDone} className={SECONDARY_BTN}>
              Cancel
            </button>
            <button
              type="button"
              onClick={onCreate}
              disabled={!valid || create.isPending}
              className={PRIMARY_BTN}
            >
              {create.isPending ? 'Creating...' : 'Create department'}
            </button>
          </div>
        </div>
      </div>
    </div>
  )
}

// ---------------------------------------------------------------------------
// Department name (inline editable when can_edit)
// ---------------------------------------------------------------------------

function DepartmentName({ department }: { department: Department }) {
  const update = useUpdateDepartment()
  const [name, setName] = useState(department.name)
  useEffect(() => setName(department.name), [department.name])

  if (!department.can_edit) {
    return <h3 className="text-sm font-semibold text-p-text truncate">{department.name}</h3>
  }

  const commit = () => {
    const trimmed = name.trim()
    if (trimmed && trimmed !== department.name) {
      update.mutate({ id: department.id, name: trimmed })
    } else {
      setName(department.name)
    }
  }

  return (
    <div className="min-w-0 flex-1">
      <input
        type="text"
        value={name}
        onChange={e => setName(e.target.value)}
        onBlur={commit}
        onKeyDown={e => {
          if (e.key === 'Enter') (e.target as HTMLInputElement).blur()
        }}
        aria-label="Department name"
        className={`${INPUT} font-semibold w-full sm:w-64`}
      />
      {update.error && <p className={`${ERROR_TEXT} mt-1`}>{update.error.message}</p>}
    </div>
  )
}

// ---------------------------------------------------------------------------
// Levels editor (local draft + explicit save)
// ---------------------------------------------------------------------------

interface DraftLevel {
  uid: string
  id: string // '' = new level
  name: string
}

let draftUidSeq = 0

function LevelsEditor({ department }: { department: Department }) {
  const setLevels = useSetDepartmentLevels()
  const server = sortedLevels(department)
  const makeDraft = (): DraftLevel[] =>
    sortedLevels(department).map(l => ({ uid: `s-${l.id}`, id: l.id, name: l.name }))

  const [draft, setDraft] = useState<DraftLevel[]>(makeDraft)
  const [confirming, setConfirming] = useState(false)
  const [unassigned, setUnassigned] = useState<string[]>([])

  // Re-sync the draft when the server levels actually change (react-query's
  // structural sharing keeps the array reference stable across identical
  // refetches, so a background poll never clobbers an in-progress edit).
  useEffect(() => {
    setDraft(makeDraft())
    setConfirming(false)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [department.levels])

  const editDraft = (updater: (d: DraftLevel[]) => DraftLevel[]) => {
    setConfirming(false)
    setDraft(updater)
  }

  const dirty = useMemo(() => {
    const a = server.map(l => `${l.id}\t${l.name}`).join('\n')
    const b = draft.map(l => `${l.id}\t${l.name}`).join('\n')
    return a !== b
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [department.levels, draft])

  const invalid = draft.some(l => !l.name.trim())

  // Levels present on the server but absent from the draft get DELETED on
  // save — and any agents still on them are dropped out of the department.
  const removedWithMembers = server
    .filter(l => !draft.some(d => d.id === l.id))
    .map(l => ({
      level: l,
      members: department.members.filter(m => m.level_id === l.id),
    }))
    .filter(r => r.members.length > 0)

  const doSave = () => {
    setConfirming(false)
    setUnassigned([])
    setLevels.mutate(
      { id: department.id, levels: draft.map(({ id, name }) => ({ id, name: name.trim() })) },
      { onSuccess: res => setUnassigned(res.unassigned_agents || []) },
    )
  }

  const onSaveClick = () => {
    if (removedWithMembers.length > 0) {
      setConfirming(true)
      return
    }
    doSave()
  }

  return (
    <div className="space-y-2">
      <div className="space-y-1.5">
        {draft.map((row, i) => (
          <div key={row.uid} className="flex items-center gap-2">
            <span className="text-xs text-p-text-light w-4 text-right shrink-0">{i + 1}</span>
            <input
              type="text"
              value={row.name}
              onChange={e =>
                editDraft(d => d.map((r, idx) => (idx === i ? { ...r, name: e.target.value } : r)))
              }
              aria-label="Level name"
              className={`${INPUT} flex-1 min-w-0`}
            />
            <button
              type="button"
              aria-label="Move level up"
              disabled={i === 0}
              onClick={() => editDraft(d => move(d, i, -1))}
              className={ICON_BTN}
            >
              ↑
            </button>
            <button
              type="button"
              aria-label="Move level down"
              disabled={i === draft.length - 1}
              onClick={() => editDraft(d => move(d, i, 1))}
              className={ICON_BTN}
            >
              ↓
            </button>
            <button
              type="button"
              aria-label="Remove level"
              disabled={draft.length <= 1}
              onClick={() => editDraft(d => d.filter((_, idx) => idx !== i))}
              className={ICON_BTN}
            >
              ×
            </button>
          </div>
        ))}
      </div>

      <div className="flex items-center gap-2">
        <button
          type="button"
          disabled={draft.length >= MAX_LEVELS}
          onClick={() => editDraft(d => [...d, { uid: `n-${draftUidSeq++}`, id: '', name: 'New level' }])}
          className={`${SECONDARY_BTN} text-xs px-2.5 py-1`}
        >
          + Add level
        </button>
        <button
          type="button"
          onClick={onSaveClick}
          disabled={!dirty || invalid || setLevels.isPending}
          className={PRIMARY_BTN}
        >
          {setLevels.isPending ? 'Saving...' : 'Save levels'}
        </button>
        {dirty && !confirming && (
          <button type="button" onClick={() => { setDraft(makeDraft()); setConfirming(false) }} className={`${SECONDARY_BTN} text-xs px-2.5 py-1`}>
            Discard
          </button>
        )}
      </div>

      {invalid && <p className="text-xs text-p-text-light">Every level needs a name.</p>}

      {confirming && (
        <div className={`${AMBER_BOX} space-y-2`}>
          {removedWithMembers.map(r => (
            <p key={r.level.id}>
              This removes level “{r.level.name}” with {r.members.length}{' '}
              {r.members.length === 1 ? 'agent' : 'agents'} (
              {r.members.map(m => m.display_name || m.name).join(', ')}) — they will drop out of
              the department.
            </p>
          ))}
          <div className="flex items-center gap-2">
            <button
              type="button"
              onClick={doSave}
              className="px-2.5 py-1 text-xs rounded-lg bg-red-600 text-white hover:bg-red-700 transition-colors"
            >
              Remove and save
            </button>
            <button
              type="button"
              onClick={() => setConfirming(false)}
              className={`${SECONDARY_BTN} text-xs px-2.5 py-1`}
            >
              Cancel
            </button>
          </div>
        </div>
      )}

      {setLevels.error && <p className={ERROR_TEXT}>{setLevels.error.message}</p>}
      {unassigned.length > 0 && <UnassignedWarning agents={unassigned} />}
    </div>
  )
}

// ---------------------------------------------------------------------------
// Members (read-only chips grouped by level)
// ---------------------------------------------------------------------------

function MemberChip({ member }: { member: DepartmentMember }) {
  return (
    <span
      title={member.accessible ? member.name : `${member.name} (not accessible to you)`}
      className={`inline-flex items-center gap-1.5 pl-1 pr-2.5 py-0.5 rounded-full border border-p-border-light bg-p-bg text-xs text-p-text ${
        member.accessible ? '' : 'opacity-50'
      }`}
    >
      <span
        className="w-5 h-5 rounded-full flex items-center justify-center text-white text-[9px] font-bold shrink-0"
        style={{ backgroundColor: member.color || '#6B7280' }}
      >
        {member.name.slice(0, 2).toUpperCase()}
      </span>
      {member.display_name || member.name}
    </span>
  )
}

function MembersByLevel({ department }: { department: Department }) {
  const groups = sortedLevels(department)
    .map(level => ({
      level,
      members: department.members.filter(m => m.level_id === level.id),
    }))
    .filter(g => g.members.length > 0)
  const orphans = department.members.filter(
    m => !department.levels.some(l => l.id === m.level_id),
  )

  if (department.members.length === 0) {
    return <p className="text-xs text-p-text-light">No agents assigned yet.</p>
  }

  return (
    <div className="space-y-2.5">
      {groups.map(g => (
        <div key={g.level.id}>
          <p className="text-xs text-p-text-light mb-1">{g.level.name}</p>
          <div className="flex flex-wrap gap-1.5">
            {g.members.map(m => (
              <MemberChip key={m.name} member={m} />
            ))}
          </div>
        </div>
      ))}
      {orphans.length > 0 && (
        <div>
          <p className="text-xs text-p-text-light mb-1">Unassigned level</p>
          <div className="flex flex-wrap gap-1.5">
            {orphans.map(m => (
              <MemberChip key={m.name} member={m} />
            ))}
          </div>
        </div>
      )}
    </div>
  )
}

// ---------------------------------------------------------------------------
// Department card
// ---------------------------------------------------------------------------

function DepartmentCard({
  department,
  onDeleted,
}: {
  department: Department
  onDeleted: (unassigned: string[]) => void
}) {
  const update = useUpdateDepartment()
  const del = useDeleteDepartment()
  // Collapsed pill by default (round 17 — the AI Engines settings idiom):
  // the header row is the summary, expanding reveals the full editor.
  const [expanded, setExpanded] = useState(false)
  const [confirmDelete, setConfirmDelete] = useState(false)
  // Local mirrors so the toggle/select respond instantly; re-synced from the
  // server snapshot after the mutation invalidates + refetches.
  const [autoDelegation, setAutoDelegation] = useState(department.auto_delegation)
  const [reach, setReach] = useState<'adjacent' | 'subtree'>(department.reach)
  useEffect(() => setAutoDelegation(department.auto_delegation), [department.auto_delegation])
  useEffect(() => setReach(department.reach), [department.reach])

  const canEdit = department.can_edit
  const levelCount = department.levels.length
  const memberCount = department.members.length

  const handleDelete = () => {
    del.mutate(
      { id: department.id },
      { onSuccess: res => onDeleted(res.unassigned_agents || []) },
    )
  }

  return (
    <div className="bg-white dark:bg-p-surface rounded-xl border border-p-border-light">
      <button
        type="button"
        className="w-full flex items-center justify-between gap-3 p-4 text-left"
        aria-expanded={expanded}
        onClick={() => setExpanded(v => !v)}
      >
        <div className="min-w-0">
          <div className="flex items-center gap-2 flex-wrap">
            <span className="font-medium text-p-text truncate">{department.name}</span>
            {!canEdit && (
              <span className="shrink-0 text-[10px] font-semibold uppercase tracking-wide px-1.5 py-0.5 rounded-sm bg-p-bg text-p-text-secondary border border-p-border-light">
                View only
              </span>
            )}
          </div>
          <div className="text-sm text-p-text-secondary">
            {memberCount} {memberCount === 1 ? 'agent' : 'agents'} · {levelCount}{' '}
            {levelCount === 1 ? 'level' : 'levels'} ·{' '}
            {department.reach === 'adjacent' ? 'adjacent reach' : 'subtree reach'}
          </div>
        </div>
        <svg
          className={`w-4 h-4 shrink-0 text-p-text-light transition-transform ${expanded ? 'rotate-180' : ''}`}
          fill="none"
          viewBox="0 0 24 24"
          stroke="currentColor"
        >
          <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M19 9l-7 7-7-7" />
        </svg>
      </button>

      {expanded && (
        <div className="px-4 pb-4 border-t border-p-border-light pt-3">
          {/* Header: name + delete — stacked on phones (the confirm row's
              text + two buttons overflowed the right edge there). */}
          <div className="flex flex-col gap-2 sm:flex-row sm:items-start sm:justify-between sm:gap-3 mb-3">
        <DepartmentName department={department} />
        {canEdit &&
          (confirmDelete ? (
            <div className="flex flex-wrap items-center gap-2 sm:shrink-0">
              <span className={ERROR_TEXT}>Delete “{department.name}”?</span>
              <button
                type="button"
                onClick={handleDelete}
                disabled={del.isPending}
                className="px-2.5 py-1 text-xs rounded-lg bg-red-600 text-white hover:bg-red-700 transition-colors disabled:opacity-40"
              >
                {del.isPending ? 'Deleting...' : 'Confirm delete'}
              </button>
              <button
                type="button"
                onClick={() => setConfirmDelete(false)}
                className={`${SECONDARY_BTN} text-xs px-2.5 py-1`}
              >
                Cancel
              </button>
            </div>
          ) : (
            <button
              type="button"
              onClick={() => setConfirmDelete(true)}
              className="shrink-0 px-2.5 py-1 text-xs rounded-lg border border-red-200 dark:border-red-700 text-red-600 dark:text-red-400 hover:bg-red-50 dark:hover:bg-red-900/20 transition-colors"
            >
              Delete department
            </button>
          ))}
      </div>
      {del.error && <p className={`${ERROR_TEXT} mb-3`}>{del.error.message}</p>}

      <div className="divide-y divide-p-border-light [&>*]:py-4 [&>*:first-child]:pt-0 [&>*:last-child]:pb-0">
        {/* Auto-delegation */}
        <div className="flex items-center justify-between gap-4">
          <div>
            <p className="text-sm font-medium text-p-text">Auto-delegation</p>
            <p className="text-xs text-p-text-light">
              Automatically enable agents to delegate work to other agents
              of the department
            </p>
          </div>
          <Toggle
            checked={autoDelegation}
            disabled={!canEdit}
            onChange={v => {
              setAutoDelegation(v)
              update.mutate({ id: department.id, auto_delegation: v })
            }}
          />
        </div>

        {/* Reach */}
        <div className="flex flex-col gap-2 sm:flex-row sm:items-center sm:justify-between sm:gap-4">
          <div>
            <p className="text-sm font-medium text-p-text">Reach</p>
            <p className="text-xs text-p-text-light">{REACH_HINT[reach]}</p>
          </div>
          <select
            value={reach}
            disabled={!canEdit}
            onChange={e => {
              const v = e.target.value as 'adjacent' | 'subtree'
              setReach(v)
              update.mutate({ id: department.id, reach: v })
            }}
            aria-label="Delegation reach"
            className={`${INPUT} w-full sm:w-56 disabled:opacity-40 disabled:cursor-not-allowed`}
          >
            <option value="adjacent">Adjacent — one level up &amp; down</option>
            <option value="subtree">Subtree — the whole department</option>
          </select>
        </div>

        {/* Levels */}
        <div>
          <p className={SECTION_LABEL}>Levels</p>
          {canEdit ? (
            <LevelsEditor department={department} />
          ) : (
            <ol className="space-y-1.5">
              {sortedLevels(department).map((l, i) => (
                <li key={l.id} className="flex items-center gap-2 text-sm text-p-text">
                  <span className="text-xs text-p-text-light w-4 text-right shrink-0">{i + 1}</span>
                  {l.name}
                </li>
              ))}
            </ol>
          )}
        </div>

        {/* Members */}
        <div>
          <p className={SECTION_LABEL}>Members</p>
          <MembersByLevel department={department} />
          <p className="text-xs text-p-text-light mt-2">
            {"Assignment lives in each agent's settings (Department field)."}
          </p>
        </div>
          </div>
        </div>
      )}
    </div>
  )
}

// ---------------------------------------------------------------------------
// Main component
// ---------------------------------------------------------------------------

export default function DepartmentsEditor() {
  const { user } = useAuth()
  const role = user?.role || 'member'
  const canCreate = role === 'admin' || role === 'creator'
  const { data: departments, isLoading } = useDepartments()
  // Transient banner after a department delete that dropped agents.
  const [deletedUnassigned, setDeletedUnassigned] = useState<string[]>([])
  // The create form hides behind the button (round 17) — the always-open
  // card dwarfed the actual department list.
  const [showCreate, setShowCreate] = useState(false)

  if (isLoading && !departments) {
    return <p className="text-sm text-p-text-secondary">Loading...</p>
  }

  const list = departments ?? []

  return (
    <div className="space-y-4">
      {/* Title + create action — stacked on phones (the explanatory
          sentence crammed 5 lines beside the button there; round 18). */}
      <div className="flex flex-col gap-3 sm:flex-row sm:items-center sm:justify-between">
        <h2 className="text-lg font-bold text-p-text">Departments</h2>
        {canCreate && !showCreate && (
          <button
            type="button"
            onClick={() => setShowCreate(true)}
            className={`${PRIMARY_BTN} shrink-0 flex items-center justify-center gap-1.5`}
          >
            <svg className="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
              <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M12 4v16m8-8H4" />
            </svg>
            New department
          </button>
        )}
      </div>
      {canCreate && showCreate && (
        <CreateDepartmentCard onDone={() => setShowCreate(false)} />
      )}

      {deletedUnassigned.length > 0 && (
        <div className={`${AMBER_BOX} flex items-start justify-between gap-3`}>
          <span>
            <strong>{deletedUnassigned.join(', ')}</strong> dropped out of the department —
            reassign them in Agent Settings.
          </span>
          <button
            type="button"
            aria-label="Dismiss"
            onClick={() => setDeletedUnassigned([])}
            className="shrink-0 leading-none text-amber-800 dark:text-amber-200 hover:opacity-70"
          >
            ×
          </button>
        </div>
      )}

      {list.length === 0 ? (
        <div className={`${CARD} text-center py-8`}>
          <p className="text-sm font-medium text-p-text">No departments yet</p>
          <p className="text-xs text-p-text-light mt-1">
            {canCreate
              ? 'Create your first one with the New department button.'
              : 'An admin or creator can set up departments.'}
          </p>
        </div>
      ) : (
        list.map(d => (
          <DepartmentCard key={d.id} department={d} onDeleted={setDeletedUnassigned} />
        ))
      )}
    </div>
  )
}
