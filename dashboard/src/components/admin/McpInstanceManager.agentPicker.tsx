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
            <label className="text-xs text-tertiary">Agents with access</label>
            {/* Available-to-all toggle */}
            <label className="flex items-center gap-2 text-xs cursor-pointer select-none">
              <span className="text-secondary">Available to all agents</span>
              <button
                type="button"
                onClick={handleToggleAll}
                aria-pressed={editing.assigned_to_all}
                aria-label="Toggle available to all agents"
                className={`w-9 h-[20px] rounded-full relative transition-colors shrink-0 ${editing.assigned_to_all ? 'bg-status-warning-solid' : 'bg-control-track'}`}
              >
                <span
                  className={`absolute top-[3px] left-[3px] w-3.5 h-3.5 rounded-full bg-action-fg shadow-elevation-1 transition-transform ${editing.assigned_to_all ? 'translate-x-[16px]' : 'translate-x-0'}`}
                />
              </button>
            </label>
          </div>
          {editing.assigned_to_all && (
            <p className="text-2xs text-status-warning-fg mb-1.5">
              All current and future agents can use this instance. The agents
              ticked below are kept but not used. Turn this off to choose agents
              one by one.
            </p>
          )}
          <div className={`flex flex-wrap gap-1.5 mt-1.5 ${editing.assigned_to_all ? 'opacity-50' : ''}`}>
            {agents.map(a => {
              const hasOther = agentsWithOtherInstance.has(a)
              return (
                <label key={a} className={`flex items-center gap-1 text-xs ${editing.assigned_to_all ? 'cursor-not-allowed' : 'cursor-pointer'} ${hasOther && editing.agents.includes(a) && !editing.assigned_to_all ? 'text-status-warning-fg' : 'text-secondary'}`}>
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
                    className="w-3.5 h-3.5 accent-action"
                  />
                  {a}
                  {hasOther && editing.agents.includes(a) && !editing.assigned_to_all && (
                    <span className="text-3xs text-status-warning-fg" title="This agent already has another instance. An instance picked for an agent comes first. Otherwise the oldest instance available to all agents is used.">!</span>
                  )}
                </label>
              )
            })}
          </div>
          {delivery === 'env' && !editing.assigned_to_all && (
            (agentsWithOtherInstance.size > 0 && editing.agents.some(a => agentsWithOtherInstance.has(a))) && (
              <p className="text-2xs text-status-warning-fg mt-1.5">
                Agents marked “!” already get this MCP from another instance.
                {otherHasAssignedToAll
                  ? ' An instance picked for an agent comes before one available to all agents. Otherwise the oldest instance is used.'
                  : ' Only the oldest instance is used for this MCP.'}
              </p>
            )
          )}
        </div>
  )
}
