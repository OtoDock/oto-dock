import { useState } from 'react'
import { useUpdateAgent, type AgentInfo } from '../../api/agents'
import { useDialogs } from '../../components/ui/dialogs'
import { Select } from '../../components/ui/Field'
import { SettingRow } from '../../components/ui/SettingsControls'
import Switch from '../../components/ui/Switch'
import { useSaveState } from '../../hooks/useSaveState'
import { ROLE, roleLabel, type AgentRole } from '../../lib/permissions'
import { isCollaborative, type VisibilityMode } from '../../lib/visibility'

export const TEAM_ACCOUNT_LABEL = "Use the agent's own accounts in every chat"
const DESCRIPTION = "People's chats then run on the accounts a manager connected for the agent."
const CONFIRM_TITLE = "Use the agent's own accounts?"
function confirmBody(floor: string): string {
  return "Every chat of this agent then uses the agent's own accounts, not each person's. "
    + `People below the ${roleLabel(floor)} role then get no account for these tools.`
}
const CONFIRM_LABEL = 'Use them'
const FLOOR_LABEL = 'From this role'
const FLOOR_HINT = "People below this role get no account for the agent's tools in their chats."
const FLOORS: AgentRole[] = [ROLE.VIEWER, ROLE.CONTRIBUTOR, ROLE.EDITOR, ROLE.MANAGER]
const OFF_TEXT = "People's chats use their own accounts."

function onText(floor: string): string {
  return `Every chat of this agent uses the agent's own accounts, from the ${roleLabel(floor).toLowerCase()} role up.`
}

/** The team account switch under the mode selector: people's chats on a
 *  collaborative agent run on the agent's own accounts, from a role floor
 *  up. Shown for the two collaborative modes; a manager sets it, everyone
 *  else reads it. */
export function TeamAccountSwitch({ name, info, mode, canManage }: {
  name: string
  info: AgentInfo
  mode: VisibilityMode
  canManage: boolean
}) {
  const dialogs = useDialogs()
  const update = useUpdateAgent()
  const { stateOf, run } = useSaveState()
  const [on, setOn] = useState(!!info.team_account)
  const [floor, setFloor] = useState<string>(info.team_account_floor || ROLE.EDITOR)
  const [shown, setShown] = useState(info)
  if (shown !== info) {
    setShown(info)
    setOn(!!info.team_account)
    setFloor(info.team_account_floor || ROLE.EDITOR)
  }
  if (!isCollaborative(mode)) return null
  if (!canManage) {
    return <p className="text-xs text-tertiary">{on ? onText(floor) : OFF_TEXT}</p>
  }

  const save = (key: string, fields: Record<string, unknown>, revert: () => void) =>
    void run(key, () => update.mutateAsync({ name, ...fields }), {
      failTitle: 'The setting was not saved', revert,
    })

  const toggle = async (next: boolean) => {
    if (next && !(await dialogs.confirm({
      title: CONFIRM_TITLE, body: confirmBody(floor), confirmLabel: CONFIRM_LABEL,
    }))) return
    const before = on
    setOn(next)
    save('team_account', { team_account: next }, () => setOn(before))
  }

  const pickFloor = (next: string) => {
    const before = floor
    setFloor(next)
    save('team_account_floor', { team_account_floor: next }, () => setFloor(before))
  }

  return (
    <div className="flex flex-col gap-3">
      <SettingRow
        label={TEAM_ACCOUNT_LABEL}
        description={DESCRIPTION}
        save={stateOf('team_account')}
        labelId="team-account-label"
      >
        <Switch aria-labelledby="team-account-label" checked={on} onChange={(v) => void toggle(v)} />
      </SettingRow>
      {on && (
        <SettingRow
          label={FLOOR_LABEL}
          description={FLOOR_HINT}
          save={stateOf('team_account_floor')}
          htmlFor="team-account-floor"
        >
          <Select id="team-account-floor" compact value={floor} onChange={(e) => pickFloor(e.target.value)}>
            {FLOORS.map((r) => <option key={r} value={r}>{roleLabel(r)}</option>)}
          </Select>
        </SettingRow>
      )}
    </div>
  )
}
