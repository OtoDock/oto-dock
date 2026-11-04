import { useState, type ReactNode } from 'react'

import {
  TASK_MACHINE_DEFAULT, useEditTaskDetails, useEditTaskMachine, useEditTaskMode, type Task,
  type TaskPermissionMode,
} from '../../api/tasks'
import { useAgentRunTargets } from '../../api/remoteMachines'
import { AUTO_REVIEW_MODE, DONT_ASK_MODE } from '../../lib/engines/modes'
import { errorText } from '../../lib/http/apiJson'
import Button from '../../components/ui/Button'
import { Input, Select } from '../../components/ui/Field'
import InfoTip from '../../components/ui/InfoTip'
import { audienceOf } from '../../components/common/audience'
import { WhoIsToldField } from '../../components/common/WhoIsTold'
import type { AgentRole } from '../../lib/permissions'
import { SCOPE } from '../../lib/visibility'
import { FormError, Modal } from './AgentTriggers.modals'


// =====================================================================
// Modals
// =====================================================================

/**
 * A task's settings, opened from its row: where it runs ("Runs on": the
 * agent's default, or one of the places the server offers for the task's
 * run person, an offline machine listed but not pickable). `extraFields` is
 * a slot under "Runs on" for further fields of the task (its mode, who is
 * told) and `leadFields` one above it (its name): the caller owns their
 * values, says they changed with `extraDirty` and saves them in
 * `onSaveExtra`. Save sends only what changed, the machine first, then the
 * slots' fields, and closes. A refusal of either shows inline and keeps the
 * dialog open.
 */
export function TaskSettingsModal({
  agent, task, onClose, leadFields, extraFields, extraDirty = false, onSaveExtra,
}: {
  agent: string
  task: Task
  onClose: () => void
  /** Rendered above "Runs on". */
  leadFields?: ReactNode
  /** Rendered under "Runs on". */
  extraFields?: ReactNode
  /** The slot's field differs from the task's. */
  extraDirty?: boolean
  /** Saves the slot's field. A throw's message shows inline. */
  onSaveExtra?: () => Promise<void>
}) {
  const { data, isError } = useAgentRunTargets(agent, task.scope, task.id)
  const current = task.target_machine || TASK_MACHINE_DEFAULT
  const [machine, setMachine] = useState(current)
  // The machine the server holds: after the slot's save failed, a second
  // Save does not send an accepted machine again.
  const [savedMachine, setSavedMachine] = useState(current)
  const edit = useEditTaskMachine()
  const [savingExtra, setSavingExtra] = useState(false)
  const busy = edit.isPending || savingExtra
  const [formError, setFormError] = useState('')
  const targets = data?.targets ?? []
  const defaultLabel = (targets.find((t) => t.is_default)
    ?? targets.find((t) => t.target === data?.default_target))?.label ?? ''
  // A machine the server no longer offers (unpaired from the scope, its
  // policy changed) stays shown while it is the task's.
  const unlisted = current !== TASK_MACHINE_DEFAULT && !targets.some((t) => t.target === current)

  const onSubmit = async (e: React.FormEvent) => {
    e.preventDefault()
    setFormError('')
    const machineDirty = machine !== savedMachine
    if (!machineDirty && !extraDirty) {
      onClose()
      return
    }
    try {
      if (machineDirty) {
        await edit.mutateAsync({ taskId: task.id, machine })
        setSavedMachine(machine)
      }
      if (extraDirty) {
        setSavingExtra(true)
        try {
          await onSaveExtra?.()
        } finally {
          setSavingExtra(false)
        }
      }
      onClose()
    } catch (err) {
      setFormError(errorText(err) || 'The task was not saved.')
    }
  }

  return (
    <Modal onClose={onClose} title={`Task settings: ${task.name}`} busy={busy}>
      <form onSubmit={onSubmit} className="space-y-3">
        {leadFields}
        <div>
          <div className="mb-1">
            <label htmlFor="task-runs-on" className="text-xs font-medium text-primary">Runs on</label>
            <InfoTip label="Runs on">
              {"When the machine is offline at run time, the run uses the agent's default."}
            </InfoTip>
          </div>
          <Select
            id="task-runs-on"
            data-testid="task-runs-on"
            value={machine}
            onChange={(e) => setMachine(e.target.value)}
            disabled={busy}
          >
            <option value={TASK_MACHINE_DEFAULT}>
              {defaultLabel ? `The agent's default: ${defaultLabel}` : "The agent's default"}
            </option>
            {targets.map((t) => (
              <option key={t.target} value={t.target} disabled={!t.reachable || !t.pickable} title={t.why_not || undefined}>
                {t.label}{!t.reachable ? ', offline' : !t.pickable ? ', not available' : ''}
              </option>
            ))}
            {unlisted && (
              <option value={current}>{task.target_machine_label || current}, current</option>
            )}
          </Select>
          {isError && (
            <p className="text-xs text-secondary mt-1">
              The places this task can run could not be read. Try again in a moment.
            </p>
          )}
        </div>
        {extraFields}
        <FormError text={formError} />
        <div className="flex justify-end gap-2 pt-2">
          <Button type="button" onClick={onClose}>
            Cancel
          </Button>
          <Button variant="primary" type="submit" disabled={busy}>
            {busy ? 'Saving…' : 'Save'}
          </Button>
        </div>
      </form>
    </Modal>
  )
}


/** A task's notification mode that sends nothing. */
const SILENT_MODE = 'none'

/**
 * The Task settings dialog the schedules page opens: the task's name, "Runs
 * on", its mode (Don't Ask or Auto, saved with `permission_mode`; a refusal,
 * such as Auto on an engine with no reviewer, shows inline) and, for a
 * shared task that notifies, who is told (`notify_audience`).
 */
export function TaskSettingsDialog({ agent, task, onClose }: {
  agent: string
  task: Task
  onClose: () => void
}) {
  const current: TaskPermissionMode = task.permission_mode === AUTO_REVIEW_MODE ? AUTO_REVIEW_MODE : DONT_ASK_MODE
  const [mode, setMode] = useState<TaskPermissionMode>(current)
  const [name, setName] = useState(task.name)
  const savedAudience = audienceOf(task.notify_audience)
  const [audience, setAudience] = useState<AgentRole>(savedAudience)
  // A silent task tells nobody: the field is left out.
  const showsAudience = task.scope === SCOPE.AGENT && task.notification_mode !== SILENT_MODE
  const editMode = useEditTaskMode()
  const editDetails = useEditTaskDetails()
  const nameDirty = name.trim() !== task.name
  const audienceDirty = showsAudience && audience !== savedAudience
  const onSaveExtra = async () => {
    if (nameDirty && !name.trim()) throw new Error('A task needs a name.')
    if (nameDirty || audienceDirty) {
      await editDetails.mutateAsync({
        taskId: task.id,
        ...(nameDirty ? { name: name.trim() } : {}),
        ...(audienceDirty ? { notify_audience: audience } : {}),
      })
    }
    if (mode !== current) await editMode.mutateAsync({ taskId: task.id, mode })
  }
  return (
    <TaskSettingsModal
      agent={agent}
      task={task}
      onClose={onClose}
      extraDirty={mode !== current || nameDirty || audienceDirty}
      onSaveExtra={onSaveExtra}
      leadFields={(
        <div>
          <label htmlFor="task-name" className="block text-xs font-medium text-primary mb-1">Name</label>
          <Input
            id="task-name"
            data-testid="task-name"
            value={name}
            onChange={(e) => setName(e.target.value)}
            disabled={editDetails.isPending}
          />
        </div>
      )}
      extraFields={(
        <>
          <div>
            <div className="mb-1">
              <label htmlFor="task-mode" className="text-xs font-medium text-primary">Mode</label>
              <InfoTip label="Mode">
                Auto has the engine&apos;s own reviewer check a tool that needs approval first.
              </InfoTip>
            </div>
            <Select
              id="task-mode"
              data-testid="task-mode"
              value={mode}
              onChange={(e) => setMode(e.target.value as TaskPermissionMode)}
              disabled={editMode.isPending}
            >
              <option value={DONT_ASK_MODE}>Don&apos;t Ask</option>
              <option value={AUTO_REVIEW_MODE}>Auto</option>
            </Select>
            <p className="text-xs text-secondary mt-1">
              Don&apos;t Ask runs every tool.
            </p>
          </div>
          {showsAudience && (
            <WhoIsToldField id="task-who-is-told" value={audience} onChange={setAudience}
                            disabled={editDetails.isPending} />
          )}
        </>
      )}
    />
  )
}
