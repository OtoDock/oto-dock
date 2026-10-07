/**
 * ChatSidePanels — AgentChat's two floating panel stacks: the right stack
 * (task metadata, plan, goal) and the left stack (meeting, todo, workflow,
 * artifact dock with the minimized document pane's chips). Presentational:
 * every value is computed by the page.
 */
import type { useChatStream } from '../../../hooks/useChatStream'
import type { useWorkspaceState } from '../../../hooks/useWorkspaceState'
import type { useArtifactWindows } from '../../../hooks/useArtifactWindows'
import type { useDocumentChips } from './ChatDocumentPane'
import type { useRunByChat } from '../../../api/runs'
import TaskMetadata from '../../../components/chat/TaskMetadata'
import PlanPanel from '../../../components/chat/plan/PlanPanel'
import TodoPanel from '../../../components/chat/plan/TodoPanel'
import GoalPanel from '../../../components/chat/plan/GoalPanel'
import WorkflowPanel from '../../../components/chat/plan/WorkflowPanel'
import MeetingIndicator from '../../../components/chat/MeetingIndicator'
import ArtifactDock from '../../../components/chat/artifacts/ArtifactDock'

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
  return (
    <>
      {/* Right-side floating panels (stacked). Hidden while the workspace
          overlay is open — they'd otherwise float over the chip row. */}
      {!workspace.state.open && (
        <div className="absolute top-14 right-3 z-10 flex flex-col gap-2 items-end">
          {/* Task chats keep the pinned run-info popup (name, status, cost). */}
          {isTaskChat && taskRun && <TaskMetadata run={taskRun} costBilled={costBilled} />}
          <PlanPanel plans={sessionPlans} />
          <GoalPanel goal={currentGoal} />
        </div>
      )}

      {/* Left-side floating panels (stacked: meeting above todo) */}
      {!workspace.state.open && (
        <div className="absolute top-14 left-3 z-10 flex flex-col gap-2 items-start">
          {meetingActive && (
            <MeetingIndicator
              participants={meetingParticipants}
              currentSpeaker={meetingSpeaker}
              leftParticipants={meetingLeftParticipants}
            />
          )}
          <TodoPanel todos={currentTodos} />
          <WorkflowPanel workflows={workflows} />
          {/* Minimized interactive-CLI artifact windows and the minimized
              document pane dock here. */}
          <ArtifactDock
            windows={artifacts.windows}
            minimized={artifacts.minimized}
            onRestore={artifacts.restore}
            onClose={artifacts.close}
            documents={documentChips}
          />
        </div>
      )}
    </>
  )
}
