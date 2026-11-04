import type { AgentRole } from '../../lib/permissions'
import { Select } from '../ui/Field'
import InfoTip from '../ui/InfoTip'
import { audienceOf, WHO_IS_TOLD } from './audience'

/** The "Who is told" select with its InfoTip (admins are always told). */
export function WhoIsToldField({ id, value, onChange, disabled = false }: {
  id: string
  value: AgentRole
  onChange: (role: AgentRole) => void
  disabled?: boolean
}) {
  return (
    <div>
      <div className="mb-1">
        <label htmlFor={id} className="text-xs font-medium text-primary">Who is told</label>
        <InfoTip label="Who is told">Admins are always told.</InfoTip>
      </div>
      <Select
        id={id}
        data-testid={id}
        value={value}
        onChange={(e) => onChange(audienceOf(e.target.value))}
        disabled={disabled}
      >
        {WHO_IS_TOLD.map((o) => (
          <option key={o.role} value={o.role}>{o.label}</option>
        ))}
      </Select>
    </div>
  )
}
