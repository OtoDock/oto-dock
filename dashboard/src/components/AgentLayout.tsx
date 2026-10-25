import { useParams } from 'react-router-dom'
import { useAuth } from '../contexts/AuthContext'
import { useAgentInfo } from '../api/agents'
import { canManageAgent } from '../lib/permissions'
import SettingsShell from './settings/SettingsShell'
import { agentSections, agentSettingsBase } from './settings/sections'

/** One agent's settings area on the settings shell. */
export default function AgentLayout() {
  const { name = '' } = useParams<{ name: string }>()
  const { user } = useAuth()
  const { data: agentInfo } = useAgentInfo(name)
  const agentDisplayName = agentInfo?.display_name || name
  const canManage = canManageAgent(user, name)

  return (
    <SettingsShell
      base={agentSettingsBase(name)}
      title={agentDisplayName}
      subtitle="Agent Settings"
      sections={agentSections(name, canManage)}
      agent={name}
    />
  )
}
