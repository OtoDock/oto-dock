// The streaming blocks of the chat stream (split from useChatStream): the
// frames of a turn in flight, each landing in the streaming bubble: text,
// thinking, tools, sub-agents and delegates, background commands,
// workflows, the permission and plan cards, a held question, the system
// cards, the media and document artifacts, the cost and context of a turn,
// the todo list and the thread goal, the usage limits.
import { useRef } from 'react'
import { useChatStore } from '@/store/chatStore'
import { notePushedDocument, pushedDocumentFromFrame } from '@/store/documentPaneStore'
import type { DisplayMessage, MessageBlock } from '../../components/chat/types'
import { answeredBetweenTurns, continuesAbove, costBilledOf, delegateResultBadge, delegateResultBlocks, delegateResultCard, delegateResultSignature, nudgeBlock, nudgeNote, reviewSpeaker, reviseOlderPlanReviews } from '../../lib/messageBlocks'
import { DELEGATE_TOOL, ROLE, toolRole } from '../../lib/tools/roles'
import { SYSTEM_SUBTYPE, WIRE, type SystemFrame } from '../../api/wireEvents'
import { DELEGATE_RESULT, type DelegateBlockStatus } from '../../lib/status/run'
import { CHAT_PHASE } from '../../lib/status/chat'
import { evictedBy } from '../../lib/kinds/artifact'
import type { WsCallbacks } from '../useDashboardWs.types'
import type { MeetingParticipant, StreamCore } from './core'

// Tools that should NOT generate tool_start/tool_end blocks (they have dedicated display
// events): the plan-mode tools by ROLE (lib/tools/roles), and the platform's delegate MCP
// tool, which renders as the (expandable) delegate pill from the proxy's delegate_spawn —
// without this skip it ALSO got a bare generic tool pill (live only; the backend never
// persisted it, so history and live disagreed).
const skipsToolEvents = (name: string): boolean => {
  const role = toolRole(name)
  return role === ROLE.PLAN_ENTER || role === ROLE.PLAN_EXIT || name === DELEGATE_TOOL
}
// How far apart the two frames of one delegate result can arrive.
const SECOND_FRAME_WINDOW_MS = 60_000
// Tools that have dedicated subagent blocks — skip tool block but keep tracking logic
const isSubagentTool = (name: string): boolean => toolRole(name) === ROLE.SUBAGENT
// The streaming bubble's open (not yet ended) thinking block, -1 when none.
const openThinkingIndex = (blocks: MessageBlock[]): number => {
  for (let i = blocks.length - 1; i >= 0; i--) {
    const b = blocks[i]
    if (b.type === WIRE.THINKING && !b.done) return i
  }
  return -1
}

export function useStreamBlocks(core: StreamCore, deps: {
  /** The queue hook's: a new block is the boundary a held steer lands at. */
  flushPendingSteer: (withContinuation?: boolean) => void
  /** The meeting hook's: the meeting subtypes of a system frame. */
  onMeetingSystem: (data: SystemFrame) => boolean
  /** The meeting hook's: the identity a permission card names in a meeting. */
  meetingParticipants: MeetingParticipant[]
}) {
  const {
    agents, chatIdRef, discardingRef, abortedRef, thinkingBufRef, messagesRef, meetingSpeakerRef, prePlanModeRef,
    mode, setMode,
    setMessages, currentMsgRef, reviewNudgeRef, appendBlock, ensureAssistantMsg, appendToLastTextBlock,
    updateToolBlock, updateToolBlockByName, updateSubagentActive, updateCommandActive, removePlaceholders,
    upsertPreviewBlock,
    setTurnStartTime, setThinkingActive, setCompressingActive, setPermissionPending, setWorkflows,
    setTotalCost, setBilledCost, setToolCost, setCostBilled, setContextUsed, setContextMax, setCacheStats, setCurrentTodos, setCurrentGoal,
    setLimitWarning, setLimitReached, setLimitReachedInfo,
  } = core
  const { flushPendingSteer, onMeetingSystem, meetingParticipants } = deps
  // One delivery can reach this socket as two frames (the ladder's pump push
  // and the socket handler's own send) moments apart: the first one renders,
  // and decides whether the streaming turn continues below the result. A
  // later round of the same task with the same output is a new result: the
  // memory is per chat and lasts only the window two frames can span.
  const seenResultsRef = useRef(new Map<string, number>())

  // The review a finished background job wakes, taken between turns: the
  // chat's socket stores its row, sends this frame and then starts the turn
  // (`server_turn_start` with the review's cause mints its bubble with this
  // line). A frame with no count (a dead session's clear) or while a turn
  // streams (a running pump's own) only clears the badges.
  const noteReviewNudge = (subtype: string, count: number | undefined) => {
    if (!count || count < 1 || currentMsgRef.current) return
    const cid = chatIdRef.current
    if (cid && useChatStore.getState().byChat[cid]?.status === CHAT_PHASE.STREAMING) return
    reviewNudgeRef.current = { subtype, count }
  }

  // The bubble the last review frame opened. Its second kind's frame comes
  // right behind the first, perhaps before the bubble is drawn.
  const reviewHostRef = useRef<string | null>(null)
  // A run's own review, inside its turn (`review` on the frame): the pump
  // stored the nudge row after the turn's blocks, so the bubble ends here and
  // the review goes on below it, joined to the reply above, as the rows draw
  // it. A second kind of the same review joins the first one's line.
  const openReviewBubble = (subtype: string, count: number | undefined) => {
    flushPendingSteer(false)  // a new block = the boundary a held steer lands at
    const block = nudgeBlock(subtype, count)
    const committed = messagesRef.current
    const last = committed[committed.length - 1]
    const open = currentMsgRef.current
    const drawn = open ? committed.find((m) => m.id === open.id) : undefined
    const sameReview = !!open && (drawn
      ? drawn === last && drawn.blocks.length > 0 && drawn.blocks.every((b) => nudgeNote(b) !== null)
      : open.id === reviewHostRef.current)
    if (sameReview) {
      appendBlock(block)
      return
    }
    const host: DisplayMessage = {
      // Its own prefix: the bubble it ends may have been minted this very
      // millisecond, and the two must not share an id.
      id: `stream-review-${Date.now()}`,
      role: 'assistant',
      blocks: [block],
      createdAt: new Date().toISOString(),
      ...reviewSpeaker(last),
    }
    currentMsgRef.current = host
    reviewHostRef.current = host.id
    setMessages((prev) => [...prev, continuesAbove(prev[prev.length - 1], host) ? { ...host, continuation: true } : host])
  }

  const callbacks: Pick<WsCallbacks,
    'onText' | 'onThinking' | 'onToolStart' | 'onToolInfo' | 'onToolResult' | 'onToolEnd' | 'onTaskSpawn'
    | 'onDelegateSpawn' | 'onDelegateResult' | 'onBgAgentDone' | 'onBgCommandSpawn' | 'onBgCommandDone'
    | 'onBgAgentsComplete' | 'onBgCommandsComplete' | 'onWorkflowStart' | 'onWorkflowProgress' | 'onWorkflowEnd'
    | 'onFgAgentsComplete' | 'onPermissionPrompt' | 'onPlanMode' | 'onPlanReview' | 'onPromptRetired'
    | 'onPermissionResolved' | 'onSystem'
    | 'onImages' | 'onImageGenerating' | 'onMcpCost' | 'onImageGenFailed' | 'onLimitWarning' | 'onLimitReached'
    | 'onUrl' | 'onFile' | 'onVideo' | 'onAudio' | 'onMediaProcessing' | 'onMediaFailed' | 'onDocumentPreview'
    | 'onQuestion' | 'onMetadata' | 'onContextCompact' | 'onTodoUpdate' | 'onGoalUpdate'> = {
    onText: (content, note = false) => {
      if (discardingRef.current || abortedRef.current) return
      if (!currentMsgRef.current) {
        if (!content) return  // the note mark opens no bubble
        const speaker = meetingSpeakerRef.current
        const msg: DisplayMessage = {
          id: `stream-${Date.now()}`,
          role: 'assistant',
          blocks: [note ? { type: 'text', content, note: true } : { type: 'text', content }],
          createdAt: new Date().toISOString(),
          ...(speaker ? {
            agentSlug: speaker.slug,
            agentDisplayName: speaker.displayName,
            agentColor: speaker.color,
            badge: 'meeting',
          } : {}),
        }
        currentMsgRef.current = msg
        setMessages((prev) => [...prev, msg])
      } else {
        appendToLastTextBlock(content, note)
      }
    },

    onThinking: (data) => {
      if (discardingRef.current || abortedRef.current) return
      flushPendingSteer()  // a new block = the boundary a held steer lands at
      ensureAssistantMsg()
      if (data.phase === 'start') {
        thinkingBufRef.current = ''
        setThinkingActive(true)
        // Open while it streams (a reasoning summary reads live), folded at its end.
        appendBlock({ type: 'thinking', content: '', collapsed: false })
      } else if (data.phase === 'progress') {
        // Live ~token gauge for content-less thinking (when adaptive effort
        // hides the thinking text): update the open thinking block's count;
        // create the block
        // if a CLI shape ever sends progress without a preceding start.
        setThinkingActive(true)
        const tokens = data.estimated_tokens || 0
        setMessages((prev) => {
          const last = prev[prev.length - 1]
          if (!last || last.role !== 'assistant') return prev
          const blocks = [...last.blocks]
          const open = openThinkingIndex(blocks)
          if (open >= 0) blocks[open] = { ...blocks[open], tokens } as MessageBlock
          else blocks.push({ type: 'thinking', content: '', collapsed: true, tokens } as MessageBlock)
          const updated = { ...last, blocks }
          return [...prev.slice(0, -1), updated]
        })
      } else if (data.phase === 'end') {
        // The phase before the text: an end may carry a last piece.
        const tail = data.text || ''
        const buffered = thinkingBufRef.current + tail
        thinkingBufRef.current = ''
        setThinkingActive(false)
        setMessages((prev) => {
          const last = prev[prev.length - 1]
          if (last && last.role === 'assistant') {
            let blocks = last.blocks
            const open = openThinkingIndex(blocks)
            if (open >= 0 && data.note) {
              // A block re-sent as a note (a Claude narration block): the note
              // text that follows draws it, so the open thinking goes.
              blocks = blocks.filter((_, i) => i !== open)
            } else if (open >= 0 && tail) {
              blocks = blocks.map((b, i) => (i === open ? { ...b, content: buffered } as MessageBlock : b))
            }
            blocks = blocks.map((b) =>
              b.type === 'thinking' ? { ...b, collapsed: true, done: true } : b,
            )
            const updated = { ...last, blocks }
            return [...prev.slice(0, -1), updated]
          }
          return prev
        })
      } else if (data.text) {
        thinkingBufRef.current += data.text
        setMessages((prev) => {
          const last = prev[prev.length - 1]
          if (last && last.role === 'assistant') {
            const blocks = [...last.blocks]
            // Only into a block still open: a delta with no start of its own
            // opens one instead of rewriting a finished block.
            const open = openThinkingIndex(blocks)
            if (open >= 0) blocks[open] = { ...blocks[open], content: thinkingBufRef.current } as MessageBlock
            else blocks.push({ type: 'thinking', content: thinkingBufRef.current, collapsed: false })
            const updated = { ...last, blocks }
            return [...prev.slice(0, -1), updated]
          }
          return prev
        })
      }
    },

    onToolStart: (data) => {
      if (discardingRef.current || abortedRef.current) return
      // Filter out plan mode tools (they have dedicated plan_mode events)
      if (skipsToolEvents(data.name)) return
      flushPendingSteer()  // a new block = the boundary a held steer lands at

      const toolId = data.tool_id || data.name

      // Agent/Task tools get a dedicated subagent block + status-bar entry —
      // both created by onTaskSpawn (the authoritative spawn event, which
      // upserts the badge keyed by tool_use_id). We do NOT pre-add the badge
      // here: a premature entry keyed by tool_id created an orphan / double-
      // count whenever task_spawn's id or ordering differed (e.g. missing/
      // empty tool_use_id, Codex mixed paths). Skip the tool block entirely;
      // the badge + block now appear together at task_spawn and clear together
      // by the same key. (No badge before task_spawn = a ~sub-second delay
      // while the tool input streams — a worthwhile trade for no desync.)
      if (isSubagentTool(data.name)) {
        return
      }

      ensureAssistantMsg()
      appendBlock({
        type: 'tool',
        name: data.name,
        toolId,
        summary: '',
        status: 'running',
      })
    },

    onToolInfo: (data) => {
      if (discardingRef.current) return
      // tool_info has name but not tool_id, so match by name
      updateToolBlockByName(data.name, { summary: data.summary || '', toolInput: data.tool_input })
    },

    onToolResult: (data) => {
      if (discardingRef.current) return
      // A FOREGROUND subagent's final report — Agent tools have no generic
      // tool block; attach the result to the subagent block (by tool_use_id)
      // so the pill expands to the report. Background spawns keep their
      // "launched" ack out of the pill (the real report arrives next turn).
      if (isSubagentTool(data.tool_name)) {
        const toolId = data.tool_use_id || ''
        if (!toolId || !data.result_content) return
        setMessages((prev) =>
          prev.map((msg) => ({
            ...msg,
            blocks: msg.blocks.map((b): MessageBlock =>
              b.type === 'subagent' && b._toolId === toolId && !b._background
                ? { ...b, toolResult: data.result_content }
                : b,
            ),
          })),
        )
        return
      }
      // Attach result content to the most recent matching tool block
      updateToolBlockByName(data.tool_name, { toolResult: data.result_content, resultSummary: data.summary })
    },

    onToolEnd: (data) => {
      if (discardingRef.current) return
      // Filter out plan mode tools
      if (skipsToolEvents(data.name)) return

      const toolId = data.tool_id || data.name

      // A subagent's tool_end means "content block streamed", NOT "agent finished".
      // Agents haven't even started executing yet. Ignore entirely.
      if (isSubagentTool(data.name)) return

      updateToolBlock(toolId, { status: 'done' })
    },

    onTaskSpawn: (data) => {
      if (discardingRef.current || abortedRef.current) return
      flushPendingSteer()  // a new block = the boundary a held steer lands at
      ensureAssistantMsg()
      // task_spawn is the AUTHORITATIVE spawn event — it creates the subagent
      // block, and the status-bar badge DERIVES from that block (keyed by
      // _toolId). The CLI binds task_spawn → tool_use_id (== the Agent tool_use
      // id from onToolStart) so bg_agent_done can clear the right block; Codex's
      // spawn_agent has no tool_use_id (_toolId null) and clears via
      // fg/bg_agents_complete.
      appendBlock({
        type: 'subagent',
        description: data.description,
        subagentType: data.subagent_type || '',
        isActive: true,  // Always start spinning — completion comes later
        _toolId: data.tool_use_id || null,
        _background: data.run_in_background || false,
        toolInput: data.tool_input,  // full Agent input — the pill expands to it
      })
    },

    onDelegateSpawn: (data) => {
      if (discardingRef.current || abortedRef.current) return
      flushPendingSteer()  // a new block = the boundary a held steer lands at
      ensureAssistantMsg()
      // Proxy-emitted on actual task-create; the badge derives from this block
      // (keyed by task_id). onDelegateResult completes it by the same task_id.
      appendBlock({
        type: 'delegate',
        taskName: data.task_name,
        agent: data.agent,
        promptPreview: data.prompt_preview,
        status: 'running',
        _taskId: data.task_id,
        prompt: data.prompt || '',  // full prompt — the pill expands to it
        workerChatId: data.chat_id || undefined,
      })
    },

    onDelegateResult: (data) => {
      if (discardingRef.current) return
      // The turn a result starts is its synthesis, never a background review.
      reviewNudgeRef.current = null
      // Mark the matching delegate block completed — by task_id when present
      // (stable key), else by task_name. The badge derives from the block.
      setMessages((prev) =>
        prev.map((msg) => ({
          ...msg,
          blocks: msg.blocks.map((b): MessageBlock =>
            b.type === 'delegate'
              && ((data.task_id && b._taskId === data.task_id)
                  || (!data.task_id && b.taskName === data.task_name))
              ? { ...b, status: ((data.status || DELEGATE_RESULT.COMPLETED) as DelegateBlockStatus) }
              : b,
          ),
        })),
      )

      // Insert delegate result as inline agent message (the worker's report
      // text and the files it attached). One delivery can reach this socket
      // as TWO frames (the ladder's pump push + the WS handler's own send) —
      // dedupe on task_id + identical blocks so the bubble renders once (a
      // recurring task's later round has different output or files and
      // still renders). A result with neither text nor files mints nothing.
      const blocks = delegateResultBlocks(data)
      if (blocks.length) {
        const delegateAgent = agents?.find(a => a.name === data.agent)
        const dedupPrefix = `delegate-result-${data.task_id || data.task_name}-`
        const signature = delegateResultSignature(blocks)
        const seenKey = `${chatIdRef.current || ''}:${dedupPrefix}${signature}`
        const now = Date.now()
        const seen = seenResultsRef.current
        for (const [key, at] of seen) if (now - at > SECOND_FRAME_WINDOW_MS) seen.delete(key)
        if (seen.has(seenKey)) return
        seen.set(seenKey, now)
        const result: DisplayMessage = {
          id: `${dedupPrefix}${Date.now()}`,
          role: 'assistant' as const,
          blocks,
          createdAt: new Date().toISOString(),
          agentSlug: data.agent || '',
          agentDisplayName: delegateAgent?.display_name,
          agentColor: delegateAgent?.color || '',
          badge: delegateResultBadge(data.status, data.report_missing === true),
          delegateCard: delegateResultCard(data.task_id || data.task_name, blocks),
        }
        // A result pushed while this chat's turn streams: the turn goes on
        // in a fresh bubble below it (the stream only grows the LAST
        // message), as the stored rows lay it out.
        const streaming = currentMsgRef.current
        const continuation: DisplayMessage | null = streaming ? {
          id: `stream-${Date.now()}`,
          role: 'assistant',
          blocks: [],
          createdAt: new Date().toISOString(),
        } : null
        if (continuation) currentMsgRef.current = continuation
        setMessages((prev) => (continuation ? [...prev, result, continuation] : [...prev, result]))
      }
    },

    onBgAgentDone: (data) => {
      if (discardingRef.current) return
      // One subagent finished (CLI: SubagentStop hook / task_notification).
      // Deterministic + order-independent: clear the block for THIS tool_use_id
      // (the badge derives from it), so parallel agents that finish out of order
      // each clear their own widget (no FIFO guessing).
      const toolId = data?.tool_use_id || ''
      if (!toolId) return
      updateSubagentActive(toolId, false)
    },

    onBgCommandSpawn: (data) => {
      if (discardingRef.current || abortedRef.current) return
      flushPendingSteer()  // a new block = the boundary a held steer lands at
      ensureAssistantMsg()
      // A backgrounded Bash — create a live bg-command block (the badge derives
      // from it, keyed by _toolId). bg_command_done clears it by the same id.
      // Rendered on top of the command's normal tool card.
      appendBlock({
        type: 'bgcommand',
        command: data.command || '',
        description: data.description || '',
        isActive: true,
        _toolId: data.tool_use_id || null,
      })
    },

    onBgCommandDone: (data) => {
      if (discardingRef.current) return
      // One background command finished (CLI task_updated — no completion hook
      // exists for bg bash). Order-independent clear by tool_use_id so
      // concurrent commands each clear their own widget.
      const toolId = data?.tool_use_id || ''
      if (!toolId) return
      updateCommandActive(toolId, false)
    },

    onBgAgentsComplete: (msg) => {
      if (discardingRef.current) return
      if (msg.review) {
        // The proxy cleared the agents it names just before (their done
        // frames), and a sibling may still run: no clear of the rest.
        if (!abortedRef.current) openReviewBubble(SYSTEM_SUBTYPE.BG_AGENTS_COMPLETED, msg.count)
        return
      }
      noteReviewNudge(SYSTEM_SUBTYPE.BG_AGENTS_COMPLETED, msg.count)
      // All background agents completed (sent by bg_nudge before auto-response).
      // Mark ALL remaining bg subagent blocks as done. Array identity is
      // preserved when nothing matched (same rationale as onDone's map).
      setMessages((prev) => {
        let changed = false
        const next = prev.map((msg) => {
          if (msg.role !== 'assistant') return msg
          const hasBg = msg.blocks.some(
            (b) => b.type === 'subagent' && b.isActive && b._background,
          )
          if (!hasBg) return msg
          const blocks = msg.blocks.map((b) =>
            b.type === 'subagent' && b.isActive && b._background
              ? { ...b, isActive: false }
              : b,
          )
          const updatedMsg = { ...msg, blocks }
          changed = true
          return updatedMsg
        })
        return changed ? next : prev
      })
    },

    onBgCommandsComplete: (msg) => {
      if (discardingRef.current) return
      if (msg.review) {
        // Mirror of the agents' review: the commands it names were cleared.
        if (!abortedRef.current) openReviewBubble(SYSTEM_SUBTYPE.BG_COMMANDS_COMPLETED, msg.count)
        return
      }
      noteReviewNudge(SYSTEM_SUBTYPE.BG_COMMANDS_COMPLETED, msg.count)
      // All background commands finished (sent by bg_command_nudge before the
      // review turn). The per-command bg_command_done can't deliver post-turn
      // (the turn's pump is gone), so this is the reliable clear — mark ALL
      // active bgcommand blocks done. Mirror of onBgAgentsComplete.
      setMessages((prev) => {
        let changed = false
        const next = prev.map((msg) => {
          if (msg.role !== 'assistant') return msg
          const hasCmd = msg.blocks.some(
            (b) => b.type === 'bgcommand' && b.isActive,
          )
          if (!hasCmd) return msg
          const blocks = msg.blocks.map((b) =>
            b.type === 'bgcommand' && b.isActive
              ? { ...b, isActive: false }
              : b,
          )
          const updatedMsg = { ...msg, blocks }
          changed = true
          return updatedMsg
        })
        return changed ? next : prev
      })
    },

    onWorkflowStart: (data) => {
      if (discardingRef.current) return
      setWorkflows((prev) => {
        if (prev.some((w) => w.toolUseId === data.tool_use_id)) return prev
        return [...prev, { toolUseId: data.tool_use_id, workflowName: data.workflow_name || '', progress: [], active: true }]
      })
    },

    onWorkflowProgress: (data) => {
      if (discardingRef.current) return
      setWorkflows((prev) => prev.map((w) =>
        w.toolUseId === data.tool_use_id ? { ...w, progress: Array.isArray(data.workflow_progress) ? data.workflow_progress : [] } : w,
      ))
    },

    onWorkflowEnd: (data) => {
      if (discardingRef.current) return
      setWorkflows((prev) => prev.map((w) =>
        w.toolUseId === data.tool_use_id ? { ...w, active: false } : w,
      ))
    },

    onFgAgentsComplete: () => {
      if (discardingRef.current) return
      // Codex path: the collab `wait` tool emits fg_agents_complete (no
      // per-agent id). Claude CLI clears each subagent individually via
      // bg_agent_done(tool_use_id). Mark ALL foreground subagent blocks done.
      setMessages((prev) =>
        prev.map((msg) => {
          if (msg.role !== 'assistant') return msg
          const hasFg = msg.blocks.some(
            (b) => b.type === 'subagent' && b.isActive && !b._background,
          )
          if (!hasFg) return msg
          const blocks = msg.blocks.map((b) =>
            b.type === 'subagent' && b.isActive && !b._background
              ? { ...b, isActive: false }
              : b,
          )
          const updatedMsg = { ...msg, blocks }
          return updatedMsg
        }),
      )
    },

    onPermissionPrompt: (data) => {
      if (discardingRef.current) return
      setPermissionPending(true)
      setTurnStartTime(null)  // Pause timer — LLM is blocked waiting
      ensureAssistantMsg()
      // Resolve meeting agent identity for the permission dialog
      const meetingAgentSlug = data.meeting_agent as string | undefined
      const meetingAgent = meetingAgentSlug
        ? (() => {
            const p = meetingParticipants.find(pp => pp.slug === meetingAgentSlug)
            return p ? { slug: p.slug, displayName: p.display_name, color: p.color } : { slug: meetingAgentSlug, displayName: meetingAgentSlug, color: '' }
          })()
        : undefined
      appendBlock({
        type: 'permission',
        requestId: data.request_id,
        toolName: data.tool_name,
        toolInput: data.tool_input,
        description: data.description,
        meetingAgent,
      })
    },

    onPlanMode: (data) => {
      if (discardingRef.current) return
      ensureAssistantMsg()
      if (data.action === 'enter') {
        appendBlock({ type: 'plan', action: 'enter', toolInput: data.tool_input })
        prePlanModeRef.current = mode
        setMode('plan')
      }
      // A plan's exit draws no card: the actionable card is its plan_review
      // (Claude's held one, Codex's answered between turns); the mode change
      // comes from plan_review_response / auto-approve.
    },

    onPlanReview: (data) => {
      if (discardingRef.current) return
      setPermissionPending(true)
      setTurnStartTime(null)  // Pause timer — LLM is blocked waiting
      ensureAssistantMsg()
      // A Codex plan's revision: its earlier cards read as revised.
      if (answeredBetweenTurns(data.tool_input)) setMessages((prev) => reviseOlderPlanReviews(prev, data.filename || ''))
      appendBlock({
        type: 'plan_review',
        requestId: data.request_id,
        plan: data.plan,
        toolInput: data.tool_input,
        filename: data.filename || '',
      })
    },

    // A prompt whose wait ended with no answer (the hook's caller went away,
    // the wait ran out, a Stop or a close released it): its permission or
    // held question card leaves, a plan review closes as cancelled (as the
    // history shows an unanswered one). Only a card that still gates acts:
    // a stale frame for an answered or unknown id changes nothing.
    onPromptRetired: (data) => {
      if (discardingRef.current || !data.request_id) return
      const rid = data.request_id
      const isCard = (b: MessageBlock) =>
        (b.type === 'permission' || b.type === 'plan_review' || b.type === 'question')
        && b.requestId === rid
      const gates = (b: MessageBlock) =>
        ((b.type === 'permission' || b.type === 'plan_review') && !b.resolved)
        || (b.type === 'question' && !!b.requestId && !b.answered)
      const before = messagesRef.current
      if (!before.some((m) => m.blocks.some((b) => isCard(b) && gates(b)))) return
      setMessages((prev) => prev.map((msg) => {
        if (!msg.blocks.some(isCard)) return msg
        const updated = {
          ...msg,
          blocks: msg.blocks.flatMap((b): MessageBlock[] => {
            if (!isCard(b)) return [b]
            return b.type === 'plan_review' ? [{ ...b, resolved: true, action: 'reject' }] : []
          }),
        }
        return updated
      }))
      if (before.some((m) => m.blocks.some((b) => gates(b) && !isCard(b)))) return
      setPermissionPending(false)
      // The turn goes on (the hook got its answer or retries): the timer
      // runs again, only while this chat streams.
      const cid = chatIdRef.current
      if (cid && useChatStore.getState().byChat[cid]?.status === CHAT_PHASE.STREAMING) {
        setTurnStartTime(Date.now())
      }
    },

    // A card answered on another socket of the chat (another tab, another
    // device): closed here the way its own answer would close it, and the
    // composer freed once no card still gates. A frame for an unknown or
    // already-answered id changes nothing (the answering tab gets it too).
    onPermissionResolved: (data) => {
      if (discardingRef.current || !data.request_id) return
      const rid = data.request_id
      const gates = (b: MessageBlock) =>
        ((b.type === 'permission' || b.type === WIRE.PLAN_REVIEW) && !b.resolved)
        || (b.type === WIRE.QUESTION && !!b.requestId && !b.answered)
      const ridOf = (b: MessageBlock) => ('requestId' in b ? b.requestId : undefined)
      const gatesHere = (b: MessageBlock) => gates(b) && ridOf(b) === rid
      const before = messagesRef.current
      if (!before.some((m) => m.blocks.some(gatesHere))) return
      setMessages((prev) => prev.map((msg) => {
        if (!msg.blocks.some(gatesHere)) return msg
        return {
          ...msg,
          blocks: msg.blocks.map((b): MessageBlock => {
            if (!gatesHere(b)) return b
            if (b.type === 'permission') return { ...b, resolved: true, approved: data.approved !== false }
            if (b.type === WIRE.PLAN_REVIEW) return { ...b, resolved: true, action: data.action || 'reject' }
            if (b.type === WIRE.QUESTION) return { ...b, answered: true }
            return b
          }),
        }
      }))
      if (before.some((m) => m.blocks.some((b) => gates(b) && ridOf(b) !== rid))) return
      setPermissionPending(false)
      const cid = chatIdRef.current
      if (cid && useChatStore.getState().byChat[cid]?.status === CHAT_PHASE.STREAMING) {
        setTurnStartTime(Date.now())
      }
    },

    onSystem: (data) => {
      if (discardingRef.current) return
      const subtype = data.subtype

      if (onMeetingSystem(data)) return

      // Non-meeting system events: default behavior. `message` carries the
      // body for informational cards (e.g. session_reseeded); `reason` picks
      // a card's wording (undelivered_input's `queued`, sent live when the
      // drive gate refuses a held message).
      ensureAssistantMsg()
      appendBlock({ type: 'system', subtype, message: data.message, reason: data.reason })
    },

    onImages: (data) => {
      if (discardingRef.current) return
      ensureAssistantMsg()
      // Remove the placeholder a gallery evicts (the first one — the inline
      // chat's own quantity rule) before appending the gallery
      setMessages(prev => {
        const last = prev[prev.length - 1]
        if (!last || last.role !== 'assistant') return prev
        const genIdx = last.blocks.findIndex(b => b.type === evictedBy(WIRE.IMAGES))
        if (genIdx === -1) return prev
        const blocks = [...last.blocks]
        blocks.splice(genIdx, 1)
        return [...prev.slice(0, -1), { ...last, blocks }]
      })
      // Normalize wire field names (snake_case from backend → camelCase for the renderer).
      const galleryImages = (data.images || []).map(it => ({
        url: it.url || undefined,
        imageData: it.image_data || undefined,
        mimeType: it.mime_type || undefined,
        caption: it.caption || undefined,
        attribution: it.attribution || undefined,
        linkUrl: it.link_url || undefined,
        downloadUrl: it.download_url || undefined,
      }))
      if (galleryImages.length > 0) {
        appendBlock({ type: 'images', images: galleryImages })
      }
    },
    onImageGenerating: (data) => {
      if (discardingRef.current) return
      ensureAssistantMsg()
      appendBlock({ type: 'image_generating', promptPreview: data.prompt_preview, model: data.model })
    },
    onMcpCost: (data) => {
      if (discardingRef.current) return
      const cost = data.cost_usd ?? 0
      if (cost > 0) {
        setTotalCost(prev => prev + cost)
        // A fee is money whatever the model runs on. The subscription note
        // stays the newest turn's, so the live view reads like a reload.
        if (data.cost_billed !== false) {
          setBilledCost(prev => prev + cost)
          setToolCost(prev => prev + cost)
        }
      }
    },
    onImageGenFailed: () => {
      if (discardingRef.current) return
      removePlaceholders(evictedBy(WIRE.IMAGE_GEN_FAILED))
    },
    onLimitWarning: (data) => {
      if (discardingRef.current) return
      setLimitWarning(data)
      setTimeout(() => setLimitWarning(null), 10000)
    },
    onLimitReached: (msg) => {
      if (discardingRef.current) return
      setLimitReachedInfo(msg)
      setLimitReached(true)
    },
    onUrl: (data) => {
      if (discardingRef.current) return
      ensureAssistantMsg()
      appendBlock({ type: 'url', url: data.url, title: data.title, description: data.description })
    },
    onFile: (data) => {
      if (discardingRef.current) return
      ensureAssistantMsg()
      appendBlock({ type: 'file', filename: data.filename, downloadUrl: data.download_url, description: data.description })
    },
    onVideo: (data) => {
      if (discardingRef.current) return
      ensureAssistantMsg()
      removePlaceholders(evictedBy(WIRE.VIDEO))
      appendBlock({
        type: 'video',
        srcKind: data.src_kind === 'token' ? 'token' : 'url',
        url: data.url || undefined,
        mediaUrl: data.media_url || undefined,
        token: data.token || undefined,
        mime: data.mime || undefined,
        caption: data.caption || undefined,
        title: data.title || undefined,
        poster: data.poster || undefined,
      })
    },
    onAudio: (data) => {
      if (discardingRef.current) return
      ensureAssistantMsg()
      removePlaceholders(evictedBy(WIRE.AUDIO))
      appendBlock({
        type: 'audio',
        srcKind: data.src_kind === 'token' ? 'token' : 'url',
        url: data.url || undefined,
        mediaUrl: data.media_url || undefined,
        token: data.token || undefined,
        mime: data.mime || undefined,
        caption: data.caption || undefined,
        title: data.title || undefined,
      })
    },
    onMediaProcessing: (data) => {
      if (discardingRef.current) return
      ensureAssistantMsg()
      appendBlock({
        type: 'media_processing',
        mediaKind: data.media_kind === 'audio' ? 'audio' : 'video',
        caption: data.caption || undefined,
      })
    },
    onMediaFailed: (_msg) => {
      if (discardingRef.current) return
      removePlaceholders(evictedBy(WIRE.MEDIA_FAILED))
    },
    onDocumentPreview: (data) => {
      if (discardingRef.current) return
      ensureAssistantMsg()
      // The turn's flush delivers the pushes: the card goes into the chat
      // and the chat's document pane opens on the newest (the frame is the
      // "turn ended with a push" signal; history never comes this way).
      upsertPreviewBlock({
        type: 'document_preview',
        filename: data.filename,
        fileId: data.file_id,
        downloadUrl: data.download_url,
        snapshotId: data.snapshot_id || undefined,
        generation: data.generation || undefined,
        version: data.version || undefined,
      })
      const doc = pushedDocumentFromFrame(data)
      const chat = data.chat_id || chatIdRef.current
      if (doc && chat) notePushedDocument(chat, doc)
    },

    onQuestion: (data) => {
      if (discardingRef.current) return
      ensureAssistantMsg()
      // A codex request_user_input carries a request_id → the held turn resumes
      // only on question_response. Claude's AskUserQuestion has none (answer =
      // fresh chat turn). A codex question keeps the composer gated + timer paused.
      if (data.request_id) setPermissionPending(true)
      appendBlock({
        type: 'question', toolName: data.tool_name, toolInput: data.tool_input,
        requestId: data.request_id,
      })
    },

    onMetadata: (data) => {
      if (discardingRef.current) return
      ensureAssistantMsg()
      const cost = data.cost_usd ?? 0
      const billed = costBilledOf(data)
      appendBlock({ type: 'metadata', costUsd: cost, durationMs: data.duration_ms ?? data.duration_api_ms ?? 0, costBilled: billed })
      if (cost > 0) {
        setTotalCost((prev) => prev + cost)
        if (billed !== false) setBilledCost((prev) => prev + cost)
      }
      setCostBilled(billed !== false)
      if (data.context_used && data.context_max) {
        setContextUsed(data.context_used)
        setContextMax(data.context_max)
      }
      setCacheStats((prev) => {
        const next = {
          cacheRead: data.cache_read ?? 0,
          cacheWrite: data.cache_write ?? 0,
          inputTokens: data.input_tokens ?? 0,
          outputTokens: data.output_tokens ?? 0,
        }
        return prev &&
          prev.cacheRead === next.cacheRead && prev.cacheWrite === next.cacheWrite &&
          prev.inputTokens === next.inputTokens && prev.outputTokens === next.outputTokens
          ? prev
          : next
      })
    },

    onContextCompact: (data) => {
      if (discardingRef.current) return
      if (data.phase === 'usage') {
        // Post-compaction gauge state (Codex reports the compacted prompt
        // size on its next tokenUsage) — counter only, no chip: the
        // 'completed' event already rendered the visible separator.
        if (data.post_tokens != null) setContextUsed(data.post_tokens)
        if (data.context_max) setContextMax(data.context_max)
      } else if (data.phase === 'started') {
        setCompressingActive(true)
      } else if (data.phase === 'completed') {
        setCompressingActive(false)
        ensureAssistantMsg()
        appendBlock({ type: 'system', subtype: SYSTEM_SUBTYPE.CONTEXT_COMPRESSED })
        // A between-turns manual compaction has no follow-up metadata frame —
        // the completed event carries the post-compaction size itself.
        if (data.post_tokens != null) setContextUsed(data.post_tokens)
      } else if (data.phase === 'failed') {
        // Manual compaction failed/unsupported — clear the badge, no separator
        // (the server sends the error toast separately).
        setCompressingActive(false)
      }
    },

    onTodoUpdate: (data) => {
      if (discardingRef.current) return
      setCurrentTodos(Array.isArray(data.todos) ? data.todos : [])
    },

    // --- Thread goal (live frame applies unconditionally — null clears) ---
    onGoalUpdate: (data) => {
      if (discardingRef.current) return
      setCurrentGoal(data.goal ?? null)
    },

  }

  return { callbacks }
}
