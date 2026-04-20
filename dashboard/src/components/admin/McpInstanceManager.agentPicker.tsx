import { type McpInstanceField } from '../../api/mcps'

export function AgentAssignmentPicker({
  isSingleInstance,
  fields,
  editing,
  setEditing,
  handleToggleAll,
  agents,
  agentsWithOtherInstance,
  delivery,
  otherHasAssignedToAll,
}: {
  isSingleInstance: boolean
  fields: McpInstanceField[]
  editing: { id?: number; instance_name: string; field_values: Record<string, string>; agents: string[]; assigned_to_all: boolean; hosted_mode: 'self_managed' | 'hosted'; managed_by?: 'admin' | 'system' }
  setEditing: (v: { id?: number; instance_name: string; field_values: Record<string, string>; agents: string[]; assigned_to_all: boolean; hosted_mode: 'self_managed' | 'hosted'; managed_by?: 'admin' | 'system' }) => void
  handleToggleAll: () => void
  agents: string[]
  agentsWithOtherInstance: Set<string>
  delivery?: string
  otherHasAssignedToAll: boolean
}) {
  return (
        <div className={!isSingleInstance || fields.length % 2 === 0 ? 'sm:col-span-2' : ''}>
          <div className="flex items-center justify-between mb-1.5">
            <label className="text-xs text-p-text-light">Agents with access</label>
            {/* Available-to-all toggle */}
            <label className="flex items-center gap-2 text-xs cursor-pointer select-none">
              <span className="text-p-text-secondary">Available to all agents</span>
              <button
                type="button"
                onClick={handleToggleAll}
                aria-pressed={editing.assigned_to_all}
                aria-label="Toggle available to all agents"
                className={`w-9 h-[20px] rounded-full relative transition-colors shrink-0 ${editing.assigned_to_all ? 'bg-amber-500' : 'bg-gray-300 dark:bg-gray-600'}`}
              >
                <span
                  className={`absolute top-[3px] left-[3px] w-3.5 h-3.5 rounded-full bg-white shadow-xs transition-transform ${editing.assigned_to_all ? 'translate-x-[16px]' : 'translate-x-0'}`}
                />
              </button>
            </label>
          </div>
          {editing.assigned_to_all && (
            <p className="text-[11px] text-amber-600 mb-1.5">
              All current and future agents can use this instance. Per-agent
              selection below is preserved but ignored at runtime — toggle off
              to restore individual control.
            </p>
          )}
          <div className={`flex flex-wrap gap-1.5 mt-1.5 ${editing.assigned_to_all ? 'opacity-50' : ''}`}>
            {agents.map(a => {
              const hasOther = agentsWithOtherInstance.has(a)
              return (
                <label key={a} className={`flex items-center gap-1 text-xs ${editing.assigned_to_all ? 'cursor-not-allowed' : 'cursor-pointer'} ${hasOther && editing.agents.includes(a) && !editing.assigned_to_all ? 'text-amber-600' : 'text-p-text-secondary'}`}>
                  <input
                    type="checkbox"
                    disabled={editing.assigned_to_all}
                    checked={editing.agents.includes(a)}
                    onChange={() => {
                      const next = editing.agents.includes(a)
                        ? editing.agents.filter(x => x !== a)
                        : [...editing.agents, a]
                      setEditing({ ...editing, agents: next })
                    }}
                    className="w-3.5 h-3.5 rounded-sm accent-brand"
                  />
                  {a}
                  {hasOther && editing.agents.includes(a) && !editing.assigned_to_all && (
                    <span className="text-[10px] text-amber-500" title="This agent already has another instance — explicit assignment takes precedence; otherwise the lowest-id 'available to all' instance is used">!</span>
                  )}
                </label>
              )
            })}
          </div>
          {delivery === 'env' && !editing.assigned_to_all && (
            (agentsWithOtherInstance.size > 0 && editing.agents.some(a => agentsWithOtherInstance.has(a))) && (
              <p className="text-[11px] text-amber-600 mt-1.5">
                Agents marked with ! already have another authorizing instance.
                {otherHasAssignedToAll
                  ? ' Explicit assignment takes precedence over "available to all"; otherwise the lowest-id instance wins.'
                  : ' Only the lowest-id instance is used at runtime for this MCP.'}
              </p>
            )
          )}
        </div>
  )
}
