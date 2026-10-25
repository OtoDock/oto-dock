import { useState } from 'react'
import { useAuth } from '../../contexts/AuthContext'
import { canManageAgent } from '../../lib/permissions'
import { toastError } from '../../lib/http/apiJson'
import {
  type VisibilityMode,
  showsAgentMemory,
  showsUserMemory,
  MODE_LABEL,
} from '../../lib/visibility'
import {
  useAgentMemorySettings,
  useSetAgentMemoryToggle,
  useClearAgentMemory,
} from '../../api/memory'
import StrongConfirmModal from '../../components/StrongConfirmModal'
import Switch from '../../components/ui/Switch'
import Button from '../../components/ui/Button'
import { CARD } from '../../components/ui/classes'
import { SettingRow } from '../../components/ui/SettingsControls'
import { useSaveState } from '../../hooks/useSaveState'

// ---------------------------------------------------------------------------
// Memory section — managers + admins only. Greyed when master toggle is off.
// ---------------------------------------------------------------------------

export function MemorySection({ name, mode }: { name: string; mode: VisibilityMode }) {
  const { user } = useAuth()
  const canManage = canManageAgent(user, name)
  if (!canManage) return null
  return <MemorySectionInner name={name} mode={mode} />
}

function MemorySectionInner({ name, mode }: { name: string; mode: VisibilityMode }) {
  const { data: agentMem } = useAgentMemorySettings(name)
  const setToggle = useSetAgentMemoryToggle(name)
  const { stateOf, run } = useSaveState()
  const clearAgent = useClearAgentMemory(name)
  const [confirmingClear, setConfirmingClear] = useState(false)

  if (!agentMem) {
    return null
  }
  // Platform master switches ride on the agent-settings response (`master`) —
  // absent (PATCH-shaped cache) means "don't grey".
  const userGreyed = agentMem.master ? !agentMem.master.user_memory_enabled : false
  const agentGreyed = agentMem.master ? !agentMem.master.agent_memory_enabled : false
  const masterOff = userGreyed && agentGreyed
  // The mode decides whether each scope exists at all. Personal only has no
  // shared agent memory; Shared only has no per-user memory. Hide the row the
  // mode can't use (the backend zeroes it regardless).
  const showUser = showsUserMemory(mode)
  const showAgent = showsAgentMemory(mode)

  return (
    <div className={`${CARD} p-4`}>
      <div className="mb-3">
        <p className="text-xs font-semibold text-secondary uppercase">Memory</p>
        <p className="text-xs text-tertiary mt-0.5">
          Agents save and update memory topic files during their sessions.
          The content loads into their system prompt on its own.
        </p>
        {masterOff && (
          <p className="text-xs text-status-warning-fg mt-1">
            Memory is disabled platform-wide. Ask an admin to enable it in Setup, System Settings.
          </p>
        )}
      </div>
      <div className="space-y-3">
        {showUser ? (
          <div className={userGreyed ? 'opacity-40' : ''}>
            <SettingRow label="User memory" info="Per-user, scoped to each user of this agent" save={stateOf('user_memory_enabled')}>
              <Switch
                aria-label="User memory"
                checked={agentMem.user_memory_enabled && !userGreyed}
                onChange={(v) => { void run('user_memory_enabled', () => setToggle.mutateAsync({ key: 'user_memory_enabled', value: v }), { failTitle: 'The memory setting was not saved' }) }}
                disabled={userGreyed}
              />
            </SettingRow>
          </div>
        ) : (
          <p className="text-xs text-tertiary italic">
            Per-user memory is unavailable in {MODE_LABEL[mode]} mode.
          </p>
        )}
        {showAgent ? (
          <div className={agentGreyed ? 'opacity-40' : ''}>
            <SettingRow label="Agent memory" description="Shared across all users of this agent" save={stateOf('agent_memory_enabled')}>
              <Switch
                aria-label="Agent memory"
                checked={agentMem.agent_memory_enabled && !agentGreyed}
                onChange={(v) => { void run('agent_memory_enabled', () => setToggle.mutateAsync({ key: 'agent_memory_enabled', value: v }), { failTitle: 'The memory setting was not saved' }) }}
                disabled={agentGreyed}
              />
            </SettingRow>
          </div>
        ) : (
          <p className="text-xs text-tertiary italic">
            Shared agent memory is unavailable in {MODE_LABEL[mode]} mode.
          </p>
        )}
      </div>
      {showAgent && (
        <div className="mt-4 pt-3 border-t border-subtle">
          <Button variant="danger" onClick={() => setConfirmingClear(true)}>
            Clear shared agent memory
          </Button>
          <p className="text-3xs text-tertiary mt-1">
            Deletes this agent's shared memory topics in knowledge/memory/.
            Users' personal memories are untouched.
          </p>
        </div>
      )}
      {confirmingClear && (
        <StrongConfirmModal
          title={`Clear ${name}'s shared agent memory`}
          description={
            <>
              This deletes every shared memory topic file of this agent
              in knowledge/memory/. Users' personal memories are NOT affected.
              Git history is preserved, so the wipe is recoverable.
            </>
          }
          confirmWord="CLEAR-AGENT-MEMORY"
          confirmLabel="Clear memory"
          busyLabel="Clearing…"
          isPending={clearAgent.isPending}
          onCancel={() => setConfirmingClear(false)}
          onConfirm={() => {
            clearAgent.mutateAsync(undefined)
              .then(() => setConfirmingClear(false))
              .catch(toastError('The memory was not cleared'))
          }}
        />
      )}
    </div>
  )
}
