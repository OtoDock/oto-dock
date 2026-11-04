/**
 * ChatSidePanels — AgentChat's two floating panel stacks: the right stack
 * (task metadata, plan, goal) and the left stack (meeting, todo, workflow),
 * and the artifact dock with the minimized document pane's chips at the end
 * of the stack on the dock's side (lib/ui/dockSide, moved by dragging a chip
 * past the middle). Presentational: every value is computed by the page.
 */
import { useState } from 'react'
import type { useChatStream } from '../../../hooks/useChatStream'
import type { useWorkspaceState } from '../../../hooks/useWorkspaceState'
import type { useArtifactWindows } from '../../../hooks/useArtifactWindows'

import type { useRunByChat } from '../../../api/runs'
import TaskMetadata from '../../../components/chat/TaskMetadata'
import PlanPanel from '../../../components/chat/plan/PlanPanel'
import TodoPanel from '../../../components/chat/plan/TodoPanel'
import GoalPanel from '../../../components/chat/plan/GoalPanel'
import WorkflowPanel from '../../../components/chat/plan/WorkflowPanel'
import MeetingIndicator from '../../../components/chat/MeetingIndicator'
import ArtifactDock from '../../../components/chat/artifacts/ArtifactDock'
import { DOCK_SIDE, useDockSide } from '../../../lib/ui/dockSide'
import { type useDocumentChips } from './useDocumentChips'

type Stream = ReturnType<typeof useChatStream>

interface Props {
  workspace: ReturnType<typeof useWorkspaceState>
  isTaskChat: boolean
  taskRun: ReturnType<typeof useRunByChat>['data']
  costBilled: Stream['costBilled']
  sessionPlans: Stream['sessionPlans']
  currentGoal: Stream['currentGoal']
  meetingActive: boolean
  meetingParticipants: Stream['meetingParticipants']
  meetingSpeaker: Stream['meetingSpeaker']
  meetingLeftParticipants: Stream['meetingLeftParticipants']
  currentTodos: Stream['currentTodos']
  workflows: Stream['workflows']
  artifacts: ReturnType<typeof useArtifactWindows>
  /** The minimized document pane's chips, null while it is not minimized. */
  documentChips: ReturnType<typeof useDocumentChips>
}

export default function ChatSidePanels({
  workspace, isTaskChat, taskRun, costBilled, sessionPlans, currentGoal,
  meetingActive, meetingParticipants, meetingSpeaker, meetingLeftParticipants,
  currentTodos, workflows, artifacts, documentChips,
}: Props) {
  const side = useDockSide()
  // A chip's drag lifts its stack above the windows and the document pane.
  const [lifted, setLifted] = useState(false)
  const stackZ = (s: string) => (lifted && side === s ? 'z-40' : 'z-10')
  // Minimized interactive-CLI artifact windows and the minimized document
  // pane dock here.
  const dock = (
    <ArtifactDock
      windows={artifacts.windows}
      minimized={artifacts.minimized}
      onRestore={artifacts.restore}
      onClose={artifacts.close}
      documents={documentChips}
      side={side}
      order={artifacts.dockOrder}
      onReorder={artifacts.setDockOrder}
      onLift={setLifted}
    />
  )
  return (
    <>
      {/* Right-side floating panels (stacked). Hidden while the workspace
          overlay is open — they'd otherwise float over the chip row. */}
      {!workspace.state.open && (
        <div className={`absolute top-14 right-3 ${stackZ(DOCK_SIDE.RIGHT)} flex flex-col gap-2 items-end`}>
          {/* Task chats keep the pinned run-info popup (name, status, cost). */}
          {isTaskChat && taskRun && <TaskMetadata run={taskRun} costBilled={costBilled} />}
          <PlanPanel plans={sessionPlans} />
          <GoalPanel goal={currentGoal} />
          {/* A terminal chat's badges (TerminalView) sit at this corner: the
              dock starts one badge row lower. A dock with no chips renders
              nothing, and its wrapper leaves the column with it. */}
          {side === DOCK_SIDE.RIGHT && (artifacts.terminal ? <div className="mt-9 empty:hidden">{dock}</div> : dock)}
        </div>
      )}

      {/* Left-side floating panels (stacked: meeting above todo) */}
      {!workspace.state.open && (
        <div className={`absolute top-14 left-3 ${stackZ(DOCK_SIDE.LEFT)} flex flex-col gap-2 items-start`}>
          {meetingActive && (
            <MeetingIndicator
              participants={meetingParticipants}
              currentSpeaker={meetingSpeaker}
              leftParticipants={meetingLeftParticipants}
            />
          )}
          <TodoPanel todos={currentTodos} />
          <WorkflowPanel workflows={workflows} />
          {side === DOCK_SIDE.LEFT && dock}
        </div>
      )}
    </>
  )
}
