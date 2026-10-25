// The entry's two contracts after the split by concern: what useChatStream
// returns to the page, and what it hands the socket. Both key lists are the
// ones the unsplit hook had.
import { describe, it, expect, vi } from 'vitest'
import { renderHook } from '@testing-library/react'
import { useChatStream } from '@/hooks/useChatStream'

const wsMock = vi.hoisted(() => ({
  streaming: false, connected: false,
  sendMessage: vi.fn(), sendPermission: vi.fn(), sendPlanReviewResponse: vi.fn(), sendQuestionResponse: vi.fn(),
  resumeChat: vi.fn(), implementPlan: vi.fn(), sendLocationResponse: vi.fn(),
  subscribe: vi.fn(() => () => {}),
}))
const captured = vi.hoisted(() => ({ cb: null as any }))
vi.mock('@/hooks/useDashboardWs', () => ({
  useDashboardWs: (cb: any) => { captured.cb = cb; return wsMock },
}))

const RETURN_KEYS = [
  'abortedRef', 'aborting', 'activeAgents', 'billedCost', 'cacheStats', 'chatId', 'chatIdRef', 'compressingActive',
  'contextMax', 'contextUsed', 'costBilled', 'currentGoal', 'currentMsgRef', 'currentTodos', 'discardingRef',
  'editText', 'erroredRef', 'finalizeAbortedTurn', 'handleImplementPlan', 'handlePermissionRespond',
  'handlePlanFetched', 'handleQuestionAnswer', 'handleQuestionAnswerStructured', 'handleSendMessage',
  'hasMoreOlder', 'limitReached', 'limitReachedInfo', 'limitWarning', 'loadOlder', 'loadingOlder', 'meetingActive',
  'meetingLeftParticipants', 'meetingMaxRounds', 'meetingParticipants', 'meetingRound', 'meetingSpeaker',
  'meetingSpeakerRef', 'messages', 'mode', 'model', 'offlineMachineName', 'pendingSteers', 'permissionPending',
  'resolvePlanReview', 'seedDbHistory', 'sendAppAction', 'sendArtifactInteraction', 'sentWithBubbleIdRef',
  'sentWithBubbleRef', 'sessionExecutionTarget', 'sessionFallbackReason', 'sessionId', 'sessionPlans',
  'setAborting', 'setBilledCost', 'setChatId', 'setCompressingActive', 'setContextMax', 'setContextUsed',
  'setCurrentGoal', 'setCurrentTodos', 'setEditText', 'setLimitReached', 'setLimitWarning', 'setMeetingActive',
  'setMeetingParticipants', 'setMeetingRound', 'setMeetingSpeaker', 'setMessages', 'setMode', 'setModel',
  'setPermissionPending', 'setSessionId', 'setSessionPlans', 'setThinkingActive', 'setToolCost', 'setTotalCost',
  'setTurnStartTime', 'setWorkflows', 'thinkingActive', 'thinkingBufRef', 'toolCost', 'totalCost', 'turnStartTime',
  'workflows', 'ws',
]

const CALLBACK_KEYS = [
  'isViewedChatPtyLive', 'newChatTarget', 'onAborted', 'onAudio', 'onBgAgentDone', 'onBgAgentsComplete',
  'onBgCommandDone',
  'onBgCommandSpawn', 'onBgCommandsComplete', 'onChatHistory', 'onChatHistoryDelta', 'onChatMoved', 'onChatRows',
  'onChatViewOnly', 'onContextCompact', 'onDelegateResult', 'onDelegateSpawn', 'onDocumentPreview', 'onDone',
  'onEngineSwitched', 'onError', 'onExecutionModeChanged', 'onFgAgentsComplete', 'onFile', 'onGoalUpdate',
  'onImageGenFailed', 'onImageGenerating', 'onImages', 'onLimitReached', 'onLimitWarning', 'onLiveState',
  'onLiveness', 'onLocationRequest', 'onMcpCost', 'onMediaFailed', 'onMediaProcessing', 'onMetadata',
  'onModeChanged', 'onModelChanged', 'onNotification', 'onNotificationCount', 'onNotificationSilent',
  'onPermissionPrompt', 'onPermissionResolved', 'onPlanMode', 'onPlanReview', 'onPlanStatus', 'onPreWarmupReady',
  'onPromptRetired',
  'onQuestion', 'onQueueEditReturn', 'onQueueRemoved', 'onQueueSent', 'onQueued', 'onServerTurnStart', 'onSteered',
  'onSwitchEngineDenied', 'onSystem', 'onTaskSpawn', 'onText', 'onThinking', 'onTitleUpdated', 'onTodoUpdate',
  'onToolEnd', 'onToolInfo', 'onToolResult', 'onToolStart', 'onTurnComplete', 'onUrl', 'onVideo', 'onWarmupFailed',
  'onWarmupReady', 'onWarmupStarted', 'onWorkflowEnd', 'onWorkflowProgress', 'onWorkflowStart', 'viewedChatId',
]

describe('useChatStream after the split', () => {
  it('returns the same keys to the page', () => {
    const { result } = renderHook(() => useChatStream({
      agents: [], initialChatId: 'chat-1', queue: { addQueued: vi.fn(), clearQueued: vi.fn() },
    }))
    expect(Object.keys(result.current).sort()).toEqual(RETURN_KEYS)
  })

  it('hands the socket the same callbacks', () => {
    renderHook(() => useChatStream({
      agents: [], initialChatId: 'chat-1', queue: { addQueued: vi.fn(), clearQueued: vi.fn() },
    }))
    expect(Object.keys(captured.cb).sort()).toEqual(CALLBACK_KEYS)
    for (const key of CALLBACK_KEYS) {
      // Not callbacks, or a page's optional one (the new chat's place).
      if (key === 'viewedChatId' || key === 'isViewedChatPtyLive' || key === 'newChatTarget') continue
      expect(typeof captured.cb[key], key).toBe('function')
    }
  })
})
