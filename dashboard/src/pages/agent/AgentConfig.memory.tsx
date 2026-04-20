import { useState } from 'react'
import { useAuth } from '../../contexts/AuthContext'
import { canManageAgent } from '../../lib/permissions'
import {
  type VisibilityMode,
  showsAgentMemory,
  showsUserMemory,
  MODE_LABEL,
} from '../../lib/visibility'
import {
  useMemorySettings,
  useAgentMemorySettings,
  useSetAgentMemoryToggle,
  useClearAgentMemory,
} from '../../api/memory'
import StrongConfirmModal from '../../components/StrongConfirmModal'
import { Toggle } from './AgentConfig.parts'

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
  const { data: master } = useMemorySettings()
  const { data: agentMem } = useAgentMemorySettings(name)
  const setToggle = useSetAgentMemoryToggle(name)
  const clearAgent = useClearAgentMemory(name)
  const [confirmingClear, setConfirmingClear] = useState(false)

  if (!master || !agentMem) {
    return null
  }
  const masterOff = !master.user_memory_enabled && !master.agent_memory_enabled
  const userGreyed = !master.user_memory_enabled
  const agentGreyed = !master.agent_memory_enabled
  // The mode decides whether each scope exists at all. Personal only has no
  // shared agent memory; Shared only has no per-user memory. Hide the row the
  // mode can't use (the backend zeroes it regardless).
  const showUser = showsUserMemory(mode)
  const showAgent = showsAgentMemory(mode)

  return (
    <div className="bg-white dark:bg-p-surface rounded-xl border border-p-border-light p-4">
      <div className="mb-3">
        <p className="text-xs font-semibold text-p-text-secondary uppercase">Memory</p>
        <p className="text-xs text-p-text-light mt-0.5">
          Agents save and maintain memory topic files inline during sessions;
          content auto-loads into their system prompt.
        </p>
        {masterOff && (
          <p className="text-xs text-amber-600 mt-1">
            Memory is disabled platform-wide. Ask an admin to enable it in Setup → Memory.
          </p>
        )}
      </div>
      <div className="space-y-3">
        {showUser ? (
          <div className={`flex flex-col gap-2 sm:flex-row sm:items-center sm:justify-between sm:gap-4 ${userGreyed ? 'opacity-40' : ''}`}>
            <div>
              <p className="text-sm font-medium text-p-text">User memory</p>
              <p className="text-xs text-p-text-light">Per-user, scoped to each user of this agent</p>
            </div>
            <Toggle
              checked={agentMem.user_memory_enabled && !userGreyed}
              onChange={(v) => setToggle.mutate({ key: 'user_memory_enabled', value: v })}
              disabled={userGreyed}
            />
          </div>
        ) : (
          <p className="text-xs text-p-text-light italic">
            Per-user memory is unavailable in {MODE_LABEL[mode]} mode.
          </p>
        )}
        {showAgent ? (
          <div className={`flex flex-col gap-2 sm:flex-row sm:items-center sm:justify-between sm:gap-4 ${agentGreyed ? 'opacity-40' : ''}`}>
            <div>
              <p className="text-sm font-medium text-p-text">Agent memory</p>
              <p className="text-xs text-p-text-light">Shared across all users of this agent</p>
            </div>
            <Toggle
              checked={agentMem.agent_memory_enabled && !agentGreyed}
              onChange={(v) => setToggle.mutate({ key: 'agent_memory_enabled', value: v })}
              disabled={agentGreyed}
            />
          </div>
        ) : (
          <p className="text-xs text-p-text-light italic">
            Shared agent memory is unavailable in {MODE_LABEL[mode]} mode.
          </p>
        )}
      </div>
      {showAgent && (
        <div className="mt-4 pt-3 border-t border-p-border-light">
          <button
            onClick={() => setConfirmingClear(true)}
            className="px-3 py-1.5 text-sm rounded-lg border border-red-300 text-red-600 hover:bg-red-50 dark:hover:bg-red-900/20 transition-colors"
          >
            Clear shared agent memory
          </button>
          <p className="text-[10px] text-p-text-light mt-1">
            Deletes this agent's shared memory topics (knowledge/memory/).
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
              (knowledge/memory/). Users' personal memories are NOT affected.
              Git history is preserved, so the wipe is recoverable.
            </>
          }
          confirmWord="CLEAR-AGENT-MEMORY"
          confirmLabel="Clear memory"
          busyLabel="Clearing…"
          isPending={clearAgent.isPending}
          onCancel={() => setConfirmingClear(false)}
          onConfirm={() => {
            clearAgent.mutate(undefined, {
              onSuccess: () => setConfirmingClear(false),
            })
          }}
        />
      )}
    </div>
  )
}
