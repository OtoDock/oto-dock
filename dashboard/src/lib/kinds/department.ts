/**
 * The department delegation wiring — mirrored from the proxy's authority
 * `proxy/storage/agents/db_departments.py` (`tests/core/test_kinds.py`
 * binds this file to it; edit both). The mode says WHICH links a department
 * wires between its levels, the reach how many levels those links span; the
 * two are independent (reach applies to whatever the mode wires). Every
 * label here is keyed by the word, so nothing compares the words.
 */
export type DepartmentMode = 'off' | 'down' | 'down_across' | 'both'
export type DepartmentReach = 'adjacent' | 'subtree'

export const DEPARTMENT_MODES: DepartmentMode[] = ['off', 'down', 'down_across', 'both']
export const DEPARTMENT_REACHES: DepartmentReach[] = ['adjacent', 'subtree']
/** What a new department gets (the API's own defaults). */
export const DEFAULT_MODE: DepartmentMode = 'down'
export const DEFAULT_REACH: DepartmentReach = 'adjacent'

export const MODE_LABEL: Record<DepartmentMode, string> = {
  off: 'Off',
  down: 'Down only',
  down_across: 'Down and across',
  both: 'All directions',
}

/** One line under each mode card; the diagram carries the rest. */
export const MODE_HINT: Record<DepartmentMode, string> = {
  off: 'No automatic delegation.',
  down: 'Each level delegates to the level below it. Nothing goes up or across.',
  down_across: 'Each level delegates to the level below it, and agents on the same level delegate to each other.',
  both: 'Each level delegates up and down, and agents on the same level delegate to each other.',
}

/** The lower-case words for summary lines. */
export const MODE_SUMMARY: Record<DepartmentMode, string> = {
  off: 'delegation off',
  down: 'down only',
  down_across: 'down and across',
  both: 'all directions',
}

/** Whether the reach changes anything under the mode. */
export const MODE_SHOWS_REACH: Record<DepartmentMode, boolean> = {
  off: false,
  down: true,
  down_across: true,
  both: true,
}

export const REACH_LABEL: Record<DepartmentReach, string> = {
  adjacent: 'One level up or down',
  subtree: 'Whole department',
}

export const REACH_HINT: Record<DepartmentReach, string> = {
  adjacent: 'Only the level directly above or below.',
  subtree: 'Every level, however far apart.',
}

export const REACH_SUMMARY: Record<DepartmentReach, string> = {
  adjacent: 'one level up or down',
  subtree: 'whole department',
}

/** "down only · one level up or down", or "delegation off". */
export function wiringSummary(mode: DepartmentMode, reach: DepartmentReach): string {
  return MODE_SHOWS_REACH[mode] ? `${MODE_SUMMARY[mode]} · ${REACH_SUMMARY[reach]}` : MODE_SUMMARY[mode]
}

/** The Department row hint on the agent config page. */
export function wiringSentence(mode: DepartmentMode, reach: DepartmentReach): string {
  return MODE_SHOWS_REACH[mode]
    ? `Auto-wires delegation within the department: ${MODE_SUMMARY[mode]}, ${REACH_SUMMARY[reach]}.`
    : 'Automatic delegation is off in this department.'
}
