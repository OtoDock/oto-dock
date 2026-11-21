import { useState } from 'react'
import { useAgentInfo, useUpdateAgent } from '../../api/agents'
import { useAuth } from '../../contexts/AuthContext'
import { canManageAgent, isAdmin as isPlatformAdmin } from '../../lib/permissions'
import StrongConfirmModal from '../../components/StrongConfirmModal'
import { CARD } from '../../components/ui/classes'
import { Input } from '../../components/ui/Field'
import { SettingRow } from '../../components/ui/SettingsControls'
import Switch from '../../components/ui/Switch'
import { useSaveState } from '../../hooks/useSaveState'

const DEFAULT_DAYS = 180
const MIN_DAYS = 7

/** How long an agent keeps its history. Managers read it, an admin sets it. */
export function HistorySection({ name }: { name: string }) {
  const { user } = useAuth()
  const { data: info } = useAgentInfo(name)
  if (!canManageAgent(user, name) || !info) return null
  const stored = info.history_retention_days ?? 0
  return isPlatformAdmin(user)
    ? <EditableHistory name={name} stored={stored} key={stored} />
    : <ReadOnlyHistory stored={stored} />
}

function ReadOnlyHistory({ stored }: { stored: number }) {
  return (
    <div className={`${CARD} p-4`}>
      <p className="text-xs font-semibold text-secondary uppercase">History</p>
      <p className="text-sm text-secondary mt-1">
        {stored
          ? `Chats and task results older than ${stored} days are deleted for good.`
          : 'This agent keeps its chats and task results for ever.'}
        {' '}An admin can change this.
      </p>
    </div>
  )
}

function EditableHistory({ name, stored }: { name: string; stored: number }) {
  const update = useUpdateAgent()
  const { stateOf, run } = useSaveState()
  const [days, setDays] = useState(String(stored || DEFAULT_DAYS))
  const [confirming, setConfirming] = useState(false)
  const enabled = stored > 0

  const save = (value: number) => run('history_retention_days',
    () => update.mutateAsync({ name, history_retention_days: value }),
    { failTitle: 'The history window was not saved' })

  // A shorter window deletes for good at the next cleanup, so it asks as
  // turning the window on does. A value the server refuses needs no question.
  const saveDays = () => {
    const value = Number(days)
    if (!enabled || !days.trim() || value === stored) return
    if (value >= MIN_DAYS && value < stored) setConfirming(true)
    else void save(value)
  }

  const cancelConfirm = () => {
    setConfirming(false)
    setDays(String(stored || DEFAULT_DAYS))
  }

  return (
    <div className={`${CARD} p-4`}>
      <div className="mb-3">
        <p className="text-xs font-semibold text-secondary uppercase">History</p>
        <p className="text-xs text-tertiary mt-0.5">
          How long this agent keeps its chats and task results.
        </p>
      </div>
      <SettingRow save={stateOf('history_retention_days')}
        label="Delete old history"
        description="Deletes for every member of this agent, and cannot be undone."
        info={<>
          Chats, task results and check results older than the period below are deleted for good,
          with their session files. A chat still in use waits. Phone calls, meetings and reports
          about replies are not touched.
        </>}
      >
        <Switch
          checked={enabled}
          aria-label="Delete old history"
          onChange={(on) => {
            if (on) setConfirming(true)
            else void save(0)
          }}
        />
      </SettingRow>
      {enabled && (
        <SettingRow save={stateOf('history_retention_days')}
          label="Days to keep history"
          htmlFor="history-retention-days"
          description="From 7 to 3650."
        >
          <Input
            id="history-retention-days"
            type="number"
            value={days}
            min={7}
            max={3650}
            onChange={(e) => setDays(e.target.value)}
            onBlur={saveDays}
            className="max-w-20 text-right"
          />
        </SettingRow>
      )}
      {confirming && (
        <StrongConfirmModal
          title={`Delete old history of ${name}`}
          description={
            <>
              Chats and task results older than {days} days are deleted for good, every day from
              now on. This covers the chats of every member of this agent.
            </>
          }
          confirmWord="DELETE-OLD-HISTORY"
          confirmLabel="Delete old history"
          busyLabel="Saving…"
          isPending={update.isPending}
          onCancel={cancelConfirm}
          onConfirm={() => {
            void save(Number(days) || DEFAULT_DAYS).finally(() => setConfirming(false))
          }}
        />
      )}
    </div>
  )
}
