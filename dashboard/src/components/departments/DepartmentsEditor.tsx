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
import { useMemo, useState } from 'react'
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
import { isCreatorOrAbove } from '../../lib/permissions'
import {
  DEFAULT_MODE,
  DEFAULT_REACH,
  DEPARTMENT_MODES,
  DEPARTMENT_REACHES,
  MODE_HINT,
  MODE_LABEL,
  REACH_HINT,
  REACH_LABEL,
  wiringSummary,
  type DepartmentMode,
  type DepartmentReach,
} from '../../lib/kinds/department'
import { ModeDiagram, ReachDiagram } from './WiringDiagrams'
import { BUTTON_BASE, BUTTON_SIZE, BUTTON_VARIANT, CARD as UI_CARD } from '../ui/classes'
import { SaveState } from '../ui/SettingsControls'
import { useSaveState, type SaveStateApi, type SaveStatus } from '../../hooks/useSaveState'

const MAX_LEVELS = 8

// ---------------------------------------------------------------------------
// Delegation wiring pickers — radio cards with a diagram each (the agent
// visibility-mode idiom), shared by the create card and every department
// card. `name` keeps each card's radios in their own group.
// ---------------------------------------------------------------------------

function WiringCard<T extends string>({
  name,
  value,
  selected,
  disabled,
  label,
  hint,
  diagram,
  onPick,
}: {
  name: string
  value: T
  selected: boolean
  disabled: boolean
  label: string
  hint: string
  diagram: (cls: string) => React.ReactNode
  onPick: (v: T) => void
}) {
  return (
    <label
      className={`flex flex-col gap-1.5 rounded-card border px-2.5 py-2 transition-colors ${
        selected ? 'border-action-subtle-hover bg-action-subtle' : 'border-subtle'
      } ${disabled ? 'opacity-50 cursor-not-allowed' : 'cursor-pointer'} ${
        !selected && !disabled ? 'hover:bg-surface-hover' : ''
      }`}
    >
      <span className="flex items-start gap-2 min-w-0">
        <input
          type="radio"
          name={name}
          value={value}
          checked={selected}
          disabled={disabled}
          onChange={() => onPick(value)}
          aria-label={label}
          className="accent-action shrink-0 mt-0.5"
        />
        <span className="text-sm font-medium leading-tight text-primary">{label}</span>
      </span>
      {diagram(selected ? 'text-link' : 'text-secondary')}
      <span className="text-xs text-tertiary">{hint}</span>
    </label>
  )
}

function ModeSelector({
  name,
  groupLabel,
  value,
  disabled = false,
  onChange,
}: {
  name: string
  groupLabel: string
  value: DepartmentMode
  disabled?: boolean
  onChange: (m: DepartmentMode) => void
}) {
  return (
    <div role="radiogroup" aria-label={groupLabel} className="grid grid-cols-2 gap-2 sm:grid-cols-4">
      {DEPARTMENT_MODES.map(m => (
        <WiringCard
          key={m}
          name={name}
          value={m}
          selected={value === m}
          disabled={disabled}
          label={MODE_LABEL[m]}
          hint={MODE_HINT[m]}
          diagram={cls => <ModeDiagram mode={m} className={cls} />}
          onPick={onChange}
        />
      ))}
    </div>
  )
}

function ReachSelector({
  name,
  groupLabel,
  value,
  disabled = false,
  onChange,
}: {
  name: string
  groupLabel: string
  value: DepartmentReach
  disabled?: boolean
  onChange: (r: DepartmentReach) => void
}) {
  return (
    <div role="radiogroup" aria-label={groupLabel} className="grid grid-cols-2 gap-2 sm:max-w-md">
      {DEPARTMENT_REACHES.map(r => (
        <WiringCard
          key={r}
          name={name}
          value={r}
          selected={value === r}
          disabled={disabled}
          label={REACH_LABEL[r]}
          hint={REACH_HINT[r]}
          diagram={cls => <ReachDiagram reach={r} className={cls} />}
          onPick={onChange}
        />
      ))}
    </div>
  )
}

/** The two wiring rows a card shows: the mode, then the reach. */
function WiringRows({
  idPrefix,
  groupPrefix,
  mode,
  reach,
  disabled = false,
  onMode,
  onReach,
  save,
}: {
  idPrefix: string
  groupPrefix: string
  mode: DepartmentMode
  reach: DepartmentReach
  disabled?: boolean
  onMode: (m: DepartmentMode) => void
  onReach: (r: DepartmentReach) => void
  /** A saved department's save state per row; the new-department form has none. */
  save?: { mode: SaveStatus; reach: SaveStatus }
}) {
  return (
    <>
      <div className="flex flex-col gap-2">
        <div>
          <p className="text-sm font-medium text-primary">Delegation{save && <SaveState state={save.mode} />}</p>
          <p className="text-xs text-tertiary">Which way agents delegate work between the levels of this department.</p>
        </div>
        <ModeSelector
          name={`${idPrefix}-mode`}
          groupLabel={`${groupPrefix}delegation mode`}
          value={mode}
          disabled={disabled}
          onChange={onMode}
        />
      </div>
      <div className="flex flex-col gap-2">
        <div>
          <p className="text-sm font-medium text-primary">Reach{save && <SaveState state={save.reach} />}</p>
          <p className="text-xs text-tertiary">How far that delegation reaches across the levels.</p>
        </div>
        <ReachSelector
          name={`${idPrefix}-reach`}
          groupLabel={`${groupPrefix}reach`}
          value={reach}
          disabled={disabled}
          onChange={onReach}
        />
      </div>
    </>
  )
}

// ---------------------------------------------------------------------------
// Shared class strings (the primitives' looks, components/ui/classes.ts)
// ---------------------------------------------------------------------------

const CARD = `${UI_CARD} p-4`
const SECTION_LABEL = 'text-xs font-semibold uppercase tracking-wider text-tertiary mb-3'
const INPUT =
  'px-2.5 py-1.5 text-sm border border-strong rounded-control bg-surface-card text-primary placeholder:text-tertiary ' +
  'focus:outline-hidden focus:border-action focus-visible:ring-2 focus-visible:ring-focus-ring/40'
const PRIMARY_BTN = `${BUTTON_BASE} ${BUTTON_VARIANT.primary} ${BUTTON_SIZE.sm}`
const SECONDARY_BTN = `${BUTTON_BASE} ${BUTTON_VARIANT.secondary} ${BUTTON_SIZE.sm}`
const SECONDARY_BTN_XS = `${BUTTON_BASE} ${BUTTON_VARIANT.secondary} ${BUTTON_SIZE.xs}`
const DANGER_BTN_XS = `${BUTTON_BASE} ${BUTTON_VARIANT.danger} ${BUTTON_SIZE.xs}`
const ICON_BTN = `${BUTTON_BASE} ${BUTTON_VARIANT.secondary} ${BUTTON_SIZE.xs} px-1.5 leading-none`
const AMBER_BOX =
  'bg-status-warning-bg border border-status-warning-border rounded-card px-3 py-2 text-xs text-status-warning-fg'
const ERROR_TEXT = 'text-xs text-status-danger-fg'

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
      <strong>{agents.join(', ')}</strong> dropped out of the department. Reassign them on
      each agent&apos;s Configuration page.
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
  const [mode, setMode] = useState<DepartmentMode>(DEFAULT_MODE)
  const [reach, setReach] = useState<DepartmentReach>(DEFAULT_REACH)

  const valid = !!name.trim() && levels.length >= 1 && levels.every(l => l.trim())

  const onCreate = () => {
    if (!valid) return
    create.mutate(
      {
        name: name.trim(),
        mode,
        reach,
        levels: levels.map(l => l.trim()),
      },
      {
        onSuccess: () => {
          setName('')
          setLevels(['Head', 'Senior', 'Junior'])
          setMode(DEFAULT_MODE)
          setReach(DEFAULT_REACH)
          onDone()
        },
      },
    )
  }

  return (
    <div className={CARD}>
      <p className={SECTION_LABEL}>New department</p>
      <div className="divide-y divide-subtle [&>*]:py-3 [&>*:first-child]:pt-0 [&>*:last-child]:pb-0">
        {/* Name */}
        <div className="flex flex-col gap-2 sm:flex-row sm:items-center sm:justify-between sm:gap-4">
          <div>
            <p className="text-sm font-medium text-primary">Name</p>
            <p className="text-xs text-tertiary">What this department is called on the map</p>
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
            <p className="text-sm font-medium text-primary">Initial levels</p>
            <p className="text-xs text-tertiary">Top rank first. You can change these later.</p>
          </div>
          <div className="space-y-1.5">
            {levels.map((lvl, i) => (
              <div key={i} className="flex items-center gap-2">
                <span className="text-xs text-tertiary w-4 text-right shrink-0">{i + 1}</span>
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
              className={SECONDARY_BTN_XS}
            >
              + Add initial level
            </button>
          </div>
        </div>

        {/* Delegation mode + reach */}
        <WiringRows
          idPrefix="new-dept"
          groupPrefix="New department "
          mode={mode}
          reach={reach}
          onMode={setMode}
          onReach={setReach}
        />

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

// The card holds the save state; the name's shows after its field, never in
// the card's toggle, whose name it would join.
function DepartmentName({ department, run, save }: { department: Department; run: SaveStateApi['run']; save: SaveStatus }) {
  const update = useUpdateDepartment()
  const [name, setName] = useState(department.name)
  // The server's name replaces the draft when it changes (in render).
  const [serverName, setServerName] = useState(department.name)
  if (serverName !== department.name) {
    setServerName(department.name)
    setName(department.name)
  }

  if (!department.can_edit) {
    return <h3 className="text-sm font-semibold text-primary truncate">{department.name}</h3>
  }

  const commit = () => {
    const trimmed = name.trim()
    if (trimmed && trimmed !== department.name) {
      void run('name', () => update.mutateAsync({ id: department.id, name: trimmed }),
        { failTitle: 'The department name was not saved' })
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
      <SaveState state={save} />
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
  const [syncedLevels, setSyncedLevels] = useState(department.levels)
  if (syncedLevels !== department.levels) {
    setSyncedLevels(department.levels)
    setDraft(makeDraft())
    setConfirming(false)
  }

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
            <span className="text-xs text-tertiary w-4 text-right shrink-0">{i + 1}</span>
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
          className={SECONDARY_BTN_XS}
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
          <button type="button" onClick={() => { setDraft(makeDraft()); setConfirming(false) }} className={SECONDARY_BTN_XS}>
            Discard
          </button>
        )}
      </div>

      {invalid && <p className="text-xs text-tertiary">Every level needs a name.</p>}

      {confirming && (
        <div className={`${AMBER_BOX} space-y-2`}>
          {removedWithMembers.map(r => (
            <p key={r.level.id}>
              This removes level “{r.level.name}” with {r.members.length}{' '}
              {r.members.length === 1 ? 'agent' : 'agents'} (
              {r.members.map(m => m.display_name || m.name).join(', ')}). They will drop out of
              the department.
            </p>
          ))}
          <div className="flex items-center gap-2">
            <button
              type="button"
              onClick={doSave}
              className={DANGER_BTN_XS}
            >
              Remove and save
            </button>
            <button
              type="button"
              onClick={() => setConfirming(false)}
              className={SECONDARY_BTN_XS}
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
      title={member.accessible ? member.name : `${member.name}: not accessible to you`}
      className={`inline-flex items-center gap-1.5 pl-1 pr-2.5 py-0.5 rounded-full border border-subtle bg-surface-page text-xs text-primary ${
        member.accessible ? '' : 'opacity-50'
      }`}
    >
      <span
        className="w-5 h-5 rounded-full flex items-center justify-center text-on-solid text-3xs font-bold shrink-0"
        style={{ backgroundColor: member.color || 'var(--color-accent-agent-fill)' }}
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
    return <p className="text-xs text-tertiary">No agents assigned yet.</p>
  }

  return (
    <div className="space-y-2.5">
      {groups.map(g => (
        <div key={g.level.id}>
          <p className="text-xs text-tertiary mb-1">{g.level.name}</p>
          <div className="flex flex-wrap gap-1.5">
            {g.members.map(m => (
              <MemberChip key={m.name} member={m} />
            ))}
          </div>
        </div>
      ))}
      {orphans.length > 0 && (
        <div>
          <p className="text-xs text-tertiary mb-1">Unassigned level</p>
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
  const { stateOf, run } = useSaveState()
  // Collapsed pill by default (round 17 — the AI Engines settings idiom):
  // the header row is the summary, expanding reveals the full editor.
  const [expanded, setExpanded] = useState(false)
  const [confirmDelete, setConfirmDelete] = useState(false)
  // Local mirrors so the pickers respond instantly; re-synced from the
  // server snapshot after the mutation invalidates + refetches.
  const [mode, setMode] = useState<DepartmentMode>(department.mode)
  const [reach, setReach] = useState<DepartmentReach>(department.reach)
  const [serverMode, setServerMode] = useState(department.mode)
  if (serverMode !== department.mode) {
    setServerMode(department.mode)
    setMode(department.mode)
  }
  const [serverReach, setServerReach] = useState(department.reach)
  if (serverReach !== department.reach) {
    setServerReach(department.reach)
    setReach(department.reach)
  }

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
    <div className="bg-surface-card rounded-card border border-subtle">
      <button
        type="button"
        className="w-full flex items-center justify-between gap-3 p-4 text-left"
        aria-expanded={expanded}
        onClick={() => setExpanded(v => !v)}
      >
        <div className="min-w-0">
          <div className="flex items-center gap-2 flex-wrap">
            <span className="font-medium text-primary truncate">{department.name}</span>
            {!canEdit && (
              <span className="shrink-0 text-3xs font-semibold uppercase tracking-wide px-1.5 py-0.5 rounded-sm bg-surface-page text-secondary border border-subtle">
                View only
              </span>
            )}
          </div>
          <div className="text-sm text-secondary">
            {memberCount} {memberCount === 1 ? 'agent' : 'agents'} · {levelCount}{' '}
            {levelCount === 1 ? 'level' : 'levels'} · {wiringSummary(department.mode, department.reach)}
          </div>
        </div>
        <svg
          className={`w-4 h-4 shrink-0 text-tertiary transition-transform ${expanded ? 'rotate-180' : ''}`}
          fill="none"
          viewBox="0 0 24 24"
          stroke="currentColor"
        >
          <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M19 9l-7 7-7-7" />
        </svg>
      </button>

      {expanded && (
        <div className="px-4 pb-4 border-t border-subtle pt-3">
          {/* Header: name + delete — stacked on phones (the confirm row's
              text + two buttons overflowed the right edge there). */}
          <div className="flex flex-col gap-2 sm:flex-row sm:items-start sm:justify-between sm:gap-3 mb-3">
        <DepartmentName department={department} run={run} save={stateOf('name')} />
        {canEdit &&
          (confirmDelete ? (
            <div className="flex flex-wrap items-center gap-2 sm:shrink-0">
              <span className={ERROR_TEXT}>Delete “{department.name}”?</span>
              <button
                type="button"
                onClick={handleDelete}
                disabled={del.isPending}
                className={DANGER_BTN_XS}
              >
                {del.isPending ? 'Deleting...' : 'Confirm delete'}
              </button>
              <button
                type="button"
                onClick={() => setConfirmDelete(false)}
                className={SECONDARY_BTN_XS}
              >
                Cancel
              </button>
            </div>
          ) : (
            <button
              type="button"
              onClick={() => setConfirmDelete(true)}
              className="shrink-0 px-2.5 py-1 text-xs rounded-control border border-status-danger-border text-status-danger-fg hover:bg-status-danger-bg transition-colors"
            >
              Delete department
            </button>
          ))}
      </div>
      {del.error && <p className={`${ERROR_TEXT} mb-3`}>{del.error.message}</p>}

      <div className="divide-y divide-subtle [&>*]:py-4 [&>*:first-child]:pt-0 [&>*:last-child]:pb-0">
        {/* Delegation mode + reach, autosaved per pick */}
        <WiringRows
          idPrefix={department.id}
          groupPrefix=""
          mode={mode}
          reach={reach}
          disabled={!canEdit}
          onMode={v => {
            setMode(v)
            void run('mode', () => update.mutateAsync({ id: department.id, mode: v }), {
              failTitle: 'The delegation setting was not saved',
              revert: () => setMode(department.mode),
            })
          }}
          onReach={v => {
            setReach(v)
            void run('reach', () => update.mutateAsync({ id: department.id, reach: v }), {
              failTitle: 'The delegation setting was not saved',
              revert: () => setReach(department.reach),
            })
          }}
          save={{ mode: stateOf('mode'), reach: stateOf('reach') }}
        />

        {/* Levels */}
        <div>
          <p className={SECTION_LABEL}>Levels</p>
          {canEdit ? (
            <LevelsEditor department={department} />
          ) : (
            <ol className="space-y-1.5">
              {sortedLevels(department).map((l, i) => (
                <li key={l.id} className="flex items-center gap-2 text-sm text-primary">
                  <span className="text-xs text-tertiary w-4 text-right shrink-0">{i + 1}</span>
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
          <p className="text-xs text-tertiary mt-2">
            {"Each agent's department is set in its settings, in the Department field."}
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
  const canCreate = isCreatorOrAbove(user)
  const { data: departments, isLoading } = useDepartments()
  // Transient banner after a department delete that dropped agents.
  const [deletedUnassigned, setDeletedUnassigned] = useState<string[]>([])
  // The create form hides behind the button (round 17) — the always-open
  // card dwarfed the actual department list.
  const [showCreate, setShowCreate] = useState(false)

  if (isLoading && !departments) {
    return <p className="text-sm text-secondary">Loading...</p>
  }

  const list = departments ?? []

  return (
    <div className="space-y-4">
      {/* Title + create action — stacked on phones (the explanatory
          sentence crammed 5 lines beside the button there; round 18). */}
      <div className="flex flex-col gap-3 sm:flex-row sm:items-center sm:justify-between">
        <h2 className="text-lg font-bold text-primary">Departments</h2>
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
            <strong>{deletedUnassigned.join(', ')}</strong> dropped out of the department.
            Reassign them on each agent&apos;s Configuration page.
          </span>
          <button
            type="button"
            aria-label="Dismiss"
            onClick={() => setDeletedUnassigned([])}
            className="shrink-0 leading-none text-status-warning-fg hover:opacity-70"
          >
            ×
          </button>
        </div>
      )}

      {list.length === 0 ? (
        <div className={`${CARD} text-center py-8`}>
          <p className="text-sm font-medium text-primary">No departments yet</p>
          <p className="text-xs text-tertiary mt-1">
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
