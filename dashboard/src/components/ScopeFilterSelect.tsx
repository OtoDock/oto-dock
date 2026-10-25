/**
 * Small scope filter dropdown shared by the agent-settings list pages
 * (Scheduled Tasks, Task History, Notifications, Meetings). Lets a user narrow
 * a mixed list down to their own user-scoped items or the agent-scoped ones —
 * mirroring the filter already on the Triggers page.
 */

import { Select } from './ui/Field'

export type ScopeFilterValue = 'all' | 'user' | 'agent'

export function ScopeFilterSelect({
  value,
  onChange,
  className = '',
}: {
  value: ScopeFilterValue
  onChange: (v: ScopeFilterValue) => void
  className?: string
}) {
  return (
    <Select
      compact
      value={value}
      onChange={(e) => onChange(e.target.value as ScopeFilterValue)}
      className={`w-max ${className}`}
    >
      <option value="all">All scopes</option>
      <option value="user">Mine</option>
      <option value="agent">Agent</option>
    </Select>
  )
}
