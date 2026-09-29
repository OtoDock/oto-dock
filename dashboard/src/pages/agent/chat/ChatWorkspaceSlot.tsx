/**
 * ChatWorkspaceSlot — the workspace-overlay branch of AgentChat's main slot:
 * a task chat's overlay reflects the TASK's operating scope, any other chat's
 * follows the agent's visibility mode. Presentational: every value is
 * computed by the page. (The Ctrl/Cmd+E shortcut that opens the overlay
 * lives in pages/agent/chat/useOverlayPanels.ts — it must stay mounted while
 * this slot is not rendered.)
 */
import type { Dispatch, SetStateAction } from 'react'
import type { AgentSummary } from '../../../api/agents'
import type { useRunByChat } from '../../../api/runs'
import type { useWorkspaceState } from '../../../hooks/useWorkspaceState'
import { hasAgentScope, isPersonalOnly, isSharedOnly, modeOfAgent } from '../../../lib/visibility'
import WorkspaceOverlay from '../../../components/workspace/WorkspaceOverlay'

interface Props {
  isTaskChat: boolean
  currentAgent: AgentSummary | undefined
  taskRun: ReturnType<typeof useRunByChat>['data']
  agentName: string
  canManageThisAgent: boolean
  canWriteThisWorkspace: boolean
  workspace: ReturnType<typeof useWorkspaceState>
  recoverRequested: boolean
  setRecoverRequested: Dispatch<SetStateAction<boolean>>
}

export default function ChatWorkspaceSlot({
  isTaskChat, currentAgent, taskRun, agentName, canManageThisAgent, canWriteThisWorkspace,
  workspace, recoverRequested, setRecoverRequested,
}: Props) {
  return isTaskChat ? (
    <div className="flex-1 min-h-0">
      {(() => {
        // The overlay reflects the TASK's operating scope, not the
        // viewer's role: agent-scoped runs → shared dirs only
        // (Knowledge read-only, no Config, no My-* — mirrors the
        // agent-scope sandbox mount); user-scoped runs → the personal
        // set plus whatever agent folders the viewer's role allows.
        const agentMode = modeOfAgent(currentAgent)
        const isAgentScope =
          isSharedOnly(agentMode) || (taskRun?.scope ?? 'agent') !== 'user'
        return (
          <WorkspaceOverlay
            agent={agentName}
            canManage={isAgentScope ? false : canManageThisAgent}
            canEdit={canWriteThisWorkspace}
            state={workspace.state}
            actions={workspace}
            topPadding
            allowedScopes={
              isAgentScope
                ? ['agent-workspace', 'agent-knowledge']
                : hasAgentScope(agentMode)
                  ? canManageThisAgent
                    ? ['my-workspace', 'my-context', 'agent-workspace', 'agent-knowledge', 'agent-config']
                    : ['my-workspace', 'my-context', 'agent-workspace', 'agent-knowledge']
                  : canManageThisAgent
                    ? ['my-workspace', 'my-context', 'agent-config']
                    : ['my-workspace', 'my-context']
            }
            defaultScope={isAgentScope ? 'agent-workspace' : 'my-workspace'}
          />
        )
      })()}
    </div>
  ) : (
    <div className="flex-1 min-h-0">
      <WorkspaceOverlay
        agent={agentName}
        canManage={canManageThisAgent}
        canEdit={canWriteThisWorkspace}
        state={workspace.state}
        actions={workspace}
        topPadding
        defaultScope={isSharedOnly(modeOfAgent(currentAgent)) ? 'agent-workspace' : 'my-workspace'}
        initialRecover={recoverRequested}
        onRecoverConsumed={() => setRecoverRequested(false)}
        // Mode decides which workspace chips exist: Shared-only has no
        // user scope (agent chips only — workspace, knowledge, config);
        // Personal-only has no shared workspace/knowledge (My chips +
        // config only); collaborative shows the default full set.
        allowedScopes={
          isSharedOnly(modeOfAgent(currentAgent))
            ? ['agent-workspace', 'agent-knowledge', 'agent-config']
            : isPersonalOnly(modeOfAgent(currentAgent))
              ? ['my-workspace', 'my-context', 'agent-config']
              : undefined
        }
      />
    </div>
  )
}
