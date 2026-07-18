import type { DisplayMessage, MessageBlock } from '../components/chat/types'
import { cleanUserMessageText } from './transcriptCleanup'

// Pure mappers from wire/DB event shapes to renderable MessageBlocks.
// Single source of truth shared by AgentChat (live chat + task chats)
// (task run view) — both reconstruct history and live-state through these.

/** Convert a live_state inline_block to a MessageBlock for reconnect rendering. */
export function liveBlockToMessageBlock(ib: any): MessageBlock | null {
  switch (ib.type) {
    case 'images':
      return {
        type: 'images',
        images: (Array.isArray(ib.images) ? ib.images : []).map((it: any) => ({
          url: it.url || undefined,
          imageData: it.image_data || undefined,
          mimeType: it.mime_type || undefined,
          caption: it.caption || undefined,
          attribution: it.attribution || undefined,
          linkUrl: it.link_url || undefined,
          downloadUrl: it.download_url || undefined,
        })),
      }
    case 'image_generating':
      return { type: 'image_generating', promptPreview: ib.prompt_preview || '', model: ib.model || '' }
    case 'url':
      return { type: 'url', url: ib.url, title: ib.title, description: ib.description || '' }
    case 'file':
      return { type: 'file', filename: ib.filename, downloadUrl: ib.download_url, description: ib.description || '' }
    case 'video':
      return { type: 'video', srcKind: ib.src_kind === 'token' ? 'token' : 'url', url: ib.url || undefined, mediaUrl: ib.media_url || undefined, token: ib.token || undefined, mime: ib.mime || undefined, caption: ib.caption || undefined, title: ib.title || undefined, poster: ib.poster || undefined }
    case 'audio':
      return { type: 'audio', srcKind: ib.src_kind === 'token' ? 'token' : 'url', url: ib.url || undefined, mediaUrl: ib.media_url || undefined, token: ib.token || undefined, mime: ib.mime || undefined, caption: ib.caption || undefined, title: ib.title || undefined }
    case 'media_processing':
      return { type: 'media_processing', mediaKind: ib.media_kind === 'audio' ? 'audio' : 'video', caption: ib.caption || undefined }
    case 'document_preview':
      return { type: 'document_preview', wopiUrl: ib.wopi_url, filename: ib.filename, fileId: ib.file_id, downloadUrl: ib.download_url, snapshotId: ib.snapshot_id || undefined, generation: ib.generation || undefined }
    case 'ui':
      return { type: 'ui', token: ib.token || '', uiUrl: ib.ui_url || '', title: ib.title || undefined, height: typeof ib.height === 'number' ? ib.height : undefined, path: ib.path || undefined }
    case 'artifact_interaction':
      return { type: 'artifact_interaction', token: ib.token || '', title: ib.title || undefined, payload: ib.payload }
    case 'app_action':
      return { type: 'app_action', appId: ib.app_id || '', slug: ib.slug || undefined, title: ib.title || undefined, actionId: ib.action_id || '', label: ib.label || undefined, prompt: ib.prompt || undefined }
    case 'question':
      return { type: 'question', toolName: ib.tool_name || '', toolInput: ib.tool_input || {}, answered: false }
    case 'thinking':
      return { type: 'thinking', content: ib.content || '', collapsed: true, done: true }
    case 'metadata':
      return { type: 'metadata', costUsd: ib.cost_usd ?? 0, durationMs: ib.duration_ms ?? 0 }
    case 'plan_mode':
      return { type: 'plan', action: ib.action || 'enter', toolInput: ib.tool_input }
    case 'system':
      return { type: 'system', subtype: ib.subtype || '', message: ib.message, agentName: ib.agent_display_name || ib.agent, agentColor: ib.agent_color }
    default:
      return null
  }
}

export function eventToBlock(evt: any, dbMessageId?: number): MessageBlock | null {
  switch (evt.type) {
    case 'images':
      return {
        type: 'images',
        images: (Array.isArray(evt.images) ? evt.images : []).map((it: any) => ({
          url: it.url || undefined,
          imageData: it.image_data || undefined,
          mimeType: it.mime_type || undefined,
          caption: it.caption || undefined,
          attribution: it.attribution || undefined,
          linkUrl: it.link_url || undefined,
          downloadUrl: it.download_url || undefined,
        })),
      }
    case 'url':
      return { type: 'url', url: evt.url, title: evt.title, description: evt.description || '' }
    case 'file':
      return { type: 'file', filename: evt.filename, downloadUrl: evt.download_url, description: evt.description || '' }
    case 'video':
      return { type: 'video', srcKind: evt.src_kind === 'token' ? 'token' : 'url', url: evt.url || undefined, mediaUrl: evt.media_url || undefined, token: evt.token || undefined, mime: evt.mime || undefined, caption: evt.caption || undefined, title: evt.title || undefined, poster: evt.poster || undefined }
    case 'audio':
      return { type: 'audio', srcKind: evt.src_kind === 'token' ? 'token' : 'url', url: evt.url || undefined, mediaUrl: evt.media_url || undefined, token: evt.token || undefined, mime: evt.mime || undefined, caption: evt.caption || undefined, title: evt.title || undefined }
    case 'document_preview':
      return { type: 'document_preview', wopiUrl: evt.wopi_url, filename: evt.filename, fileId: evt.file_id, downloadUrl: evt.download_url, dbMessageId, snapshotId: evt.snapshot_id || undefined, generation: evt.generation || undefined }
    case 'ui':
      return { type: 'ui', token: evt.token || '', uiUrl: evt.ui_url || '', title: evt.title || undefined, height: typeof evt.height === 'number' ? evt.height : undefined, path: evt.path || undefined }
    case 'artifact_interaction':
      return { type: 'artifact_interaction', token: evt.token || '', title: evt.title || undefined, payload: evt.payload }
    case 'app_action':
      return { type: 'app_action', appId: evt.app_id || '', slug: evt.slug || undefined, title: evt.title || undefined, actionId: evt.action_id || '', label: evt.label || undefined, prompt: evt.prompt || undefined }
    case 'tool':
      // The synthesized Task-tool checklist snapshot (persisted with panel_only) is
      // restore-only: the TaskCreate/TaskUpdate calls already render their own inline
      // cards, so suppress this one inline. It still drives the panel restore via the
      // server's get_last_todo_snapshot. (Codex's synthesized block omits panel_only —
      // it IS its only inline representation — and native TodoWrite renders normally.)
      if (evt.panel_only) return null
      return {
        type: 'tool',
        name: evt.name || '',
        toolId: evt.tool_id || evt.name || '',
        summary: evt.summary || '',
        status: 'done',  // Historical = always done
        toolInput: evt.tool_input,
        toolResult: evt.tool_result,
        resultSummary: evt.result_summary,
      }
    case 'task_spawn':
      return {
        type: 'subagent',
        description: evt.description || '',
        subagentType: evt.subagent_type || '',
        isActive: false,  // Historical = completed
        _background: !!evt.run_in_background,  // keep the bg type (orange, not blue) on DB reload
        _toolId: evt.tool_use_id || undefined,
        toolInput: evt.tool_input,      // full Agent input (expandable pill)
        toolResult: evt.tool_result,    // fg subagent report, when attached
      }
    case 'bg_command_spawn':
      return {
        type: 'bgcommand',
        command: evt.command || '',
        description: evt.description || '',
        isActive: false,  // Historical = finished (spawn-only persist, mirrors subagents)
        _toolId: evt.tool_use_id || undefined,
      }
    case 'delegate_spawn':
      return {
        type: 'delegate',
        taskName: evt.task_name || '',
        agent: evt.agent || '',
        promptPreview: evt.prompt_preview || '',
        status: 'running',  // Default running — post-processing marks completed via delegate_result
        _taskId: evt.task_id || undefined,
        prompt: evt.prompt || '',       // full prompt (expandable pill)
        workerChatId: evt.chat_id || undefined,  // chat-surface lane → open-lane link
      }
    case 'delegate_result':
      return null  // Handled by post-processing (marks matching delegate_spawn as completed)
    case 'schedule_wake':
      // A scheduled self-continuation drove this turn — show the wake row.
      return { type: 'schedulewake', prompt: evt.prompt || '' }
    case 'thinking':
      return { type: 'thinking', content: evt.content || '', collapsed: true, done: true }
    case 'permission_prompt':
      return {
        type: 'permission',
        requestId: evt.request_id || '',
        toolName: evt.tool_name || '',
        toolInput: evt.tool_input || {},
        description: evt.description,
        resolved: true,
        approved: evt.approved !== false,  // Default true unless explicitly rejected (dead session)
      }
    case 'question':
      return { type: 'question', toolName: evt.tool_name || '', toolInput: evt.tool_input || {}, answered: false }
    case 'plan_mode':
      return { type: 'plan', action: evt.action || 'enter', toolInput: evt.tool_input }
    case 'plan_review':
      return {
        type: 'plan_review',
        requestId: evt.request_id || '',
        plan: evt.plan || '',
        toolInput: evt.tool_input || {},
        filename: evt.filename || '',
        resolved: true,  // Historical = already resolved
        action: evt.action || 'reject',  // No action saved = was never resolved = cancelled
      }
    case 'system':
      return { type: 'system', subtype: evt.subtype || '', message: evt.message, agentName: evt.agent_display_name || evt.agent, agentColor: evt.agent_color }
    case 'metadata':
      return { type: 'metadata', costUsd: evt.cost_usd ?? 0, durationMs: evt.duration_ms ?? evt.duration_api_ms ?? 0 }
    case 'bg_nudge':
      return { type: 'system', subtype: 'bg_agents_completed' }
    case 'bg_command_nudge':
      return { type: 'system', subtype: 'bg_commands_completed' }
    case 'fg_agents_complete':
    case 'bg_agent_done':
      return null  // Status update, no block
    default:
      return null
  }
}

/**
 * Rebuild the renderable message list from persisted chat rows — the DB
 * `messages` array delivered by chat_history (live resume) or by
 * `GET /v1/chats/{id}`. Pure (same input → same output), so it is the single
 * source of truth shared by useChatStream.onChatHistory and the interactive
 * view-toggle (terminal ⇄ rich DB history). `agents`
 * supplies delegate identity (display_name + color) — pass useAgents()'s data.
 */
export function dbMessagesToDisplay(
  dbMessages: any[],
  agents: Array<{ name: string; display_name?: string; color?: string }> | undefined,
): DisplayMessage[] {
  const displayMsgs: DisplayMessage[] = []
  // Events that signal a new LLM turn (delegate result delivery, bg agent nudge)
  let newTurnNext = false
  for (const m of dbMessages) {
    if (m.role === 'user') {
      const blocks: MessageBlock[] = []
      let agentMeta: { agent_slug?: string; agent_display_name?: string; agent_color?: string; badge?: string } = {}
      // Check for image attachments and agent identity in event_data
      if (m.event_data) {
        try {
          const ed = JSON.parse(m.event_data)
          if (Array.isArray(ed.files) && ed.files.length) {
            blocks.push({ type: 'file_attachments', files: ed.files.map((f: { name: string; path?: string }) => ({ name: f.name, path: f.path })) })
          }
          if (Array.isArray(ed.images) && ed.images.length) {
            blocks.push({
              type: 'image_attachments',
              images: ed.images.map((i: { name: string }) => i.name),
              // saved upload paths — rows persisted before paths were stored
              // carry none, and the renderer falls back to the count badge
              paths: ed.images.map((i: { path?: string }) => i.path ?? null),
            })
          }
          if (ed.agent_slug) agentMeta = ed
        } catch { /* ignore */ }
      }
      // Render-side cleanup (data untouched): injected time preludes strip off
      // the bubble; pure slash-command records hide the whole row — unless it
      // carries attachments, which keep the row alive without the noise text.
      const cleanedText = cleanUserMessageText(m.content)
      if (cleanedText === null && blocks.length === 0) continue
      if (cleanedText !== null) blocks.push({ type: 'text', content: cleanedText })
      displayMsgs.push({
        id: `db-${m.id}`,
        role: 'user',
        blocks,
        createdAt: m.created_at,
        ...(agentMeta.agent_slug ? {
          agentSlug: agentMeta.agent_slug,
          agentDisplayName: agentMeta.agent_display_name,
          agentColor: agentMeta.agent_color,
          badge: agentMeta.badge,
        } : {}),
      })
      newTurnNext = false
    } else if (m.role === 'assistant') {
      // Merge with preceding assistant message (same turn, split by events)
      // BUT start a new message if a turn-boundary event preceded this
      const last = displayMsgs[displayMsgs.length - 1]
      // Check for agent identity in event_data (meeting messages)
      let msgAgentSlug: string | undefined
      let msgAgentDisplayName: string | undefined
      let msgAgentColor: string | undefined
      let msgBadge: string | undefined
      if (m.event_data) {
        try {
          const ed = JSON.parse(m.event_data)
          msgAgentSlug = ed.agent_slug
          msgAgentDisplayName = ed.agent_display_name
          msgAgentColor = ed.agent_color
          msgBadge = ed.badge
        } catch { /* ignore */ }
      }
      // Force new message if: turn boundary, or different agent identity
      const forceNew = newTurnNext || (
        msgAgentSlug && last?.role === 'assistant' && msgAgentSlug !== last.agentSlug
      )
      if (last && last.role === 'assistant' && !forceNew) {
        if (m.content) last.blocks.push({ type: 'text', content: m.content })
      } else {
        displayMsgs.push({
          id: `db-${m.id}`,
          role: 'assistant',
          blocks: m.content ? [{ type: 'text', content: m.content }] : [],
          createdAt: m.created_at,
          agentSlug: msgAgentSlug,
          agentDisplayName: msgAgentDisplayName,
          agentColor: msgAgentColor,
          badge: msgBadge,
        })
      }
      newTurnNext = false
    } else if (m.role === 'event' && m.event_data) {
      // delegate_result / bg_nudge / artifact_interaction / app_action signal a new LLM turn
      if (m.event_type === 'delegate_result' || m.event_type === 'bg_nudge' || m.event_type === 'bg_command_nudge' || m.event_type === 'artifact_interaction' || m.event_type === 'app_action') {
        newTurnNext = true
      }
      // Meeting turn start: force new message with agent identity
      let meetingTurnAgent: { slug: string; displayName: string; color: string } | null = null
      if (m.event_type === 'system') {
        try {
          const sysEd = JSON.parse(m.event_data || '{}')
          if (sysEd.subtype === 'meeting_turn_start') {
            newTurnNext = true
            meetingTurnAgent = {
              slug: sysEd.agent || '',
              displayName: sysEd.agent_display_name || sysEd.agent || '',
              color: sysEd.agent_color || '',
            }
          }
        } catch { /* ignore */ }
      }
      try {
        const evt = JSON.parse(m.event_data)
        // Skip dismissed events
        if (evt.dismissed) continue
        const block = eventToBlock(evt, m.id)
        // Find or create the host assistant message — only when the event
        // actually renders an inline block (or opens a meeting identity
        // group). An event with no block (delegate_result, nudges, panel-only
        // tools) must NOT mint a host: an empty assistant message renders as
        // a stuck typing-dots stub, and consuming newTurnNext on it made the
        // NEXT assistant row — the delegating agent's synthesis echo after a
        // delegate_result — merge into the delegate-response bubble below,
        // reading as the delegate's own text. With no host minted the armed
        // boundary survives to the next row, which starts its own message.
        if (block || meetingTurnAgent || evt._meeting_agent) {
          let lastAssistant = displayMsgs[displayMsgs.length - 1]
          if (newTurnNext || !lastAssistant || lastAssistant.role !== 'assistant') {
            lastAssistant = {
              id: `db-evt-${m.id}`,
              role: 'assistant',
              blocks: [],
              createdAt: m.created_at,
            }
            // Carry agent identity from meeting_turn_start so text blocks merge correctly
            if (meetingTurnAgent) {
              lastAssistant.agentSlug = meetingTurnAgent.slug
              lastAssistant.agentDisplayName = meetingTurnAgent.displayName
              lastAssistant.agentColor = meetingTurnAgent.color
              lastAssistant.badge = 'meeting'
            } else if (evt._meeting_agent) {
              // A mid-turn user message split the speaker's turn: the next
              // persisted row is this EVENT (tool card / artifact), so no
              // meeting_turn_start opens the group. The pump stamps the
              // speaker slug on every persisted meeting block — resolve the
              // display identity like the delegate-result branch does.
              const speaker = agents?.find(a => a.name === evt._meeting_agent)
              lastAssistant.agentSlug = evt._meeting_agent
              lastAssistant.agentDisplayName = speaker?.display_name
              lastAssistant.agentColor = speaker?.color || ''
              lastAssistant.badge = 'meeting'
            }
            displayMsgs.push(lastAssistant)
            newTurnNext = false
          }
          if (block) lastAssistant.blocks.push(block)
        }
        // Delegate result with output: insert as separate agent message.
        // output_text is non-empty for failed/canceled terminals too (the
        // backend synthesizes a ⚠ marker), so this never mints an empty bubble.
        if (m.event_type === 'delegate_result' && evt.output_text) {
          const delegateAgent = agents?.find(a => a.name === evt.agent)
          displayMsgs.push({
            id: `db-delresult-${m.id}`,
            role: 'assistant',
            blocks: [{ type: 'text', content: evt.output_text }],
            createdAt: m.created_at,
            agentSlug: evt.agent || '',
            agentDisplayName: delegateAgent?.display_name,
            agentColor: delegateAgent?.color || '',
            badge: evt.status === 'cancelled' ? 'delegate canceled'
              : evt.status === 'failed' ? 'delegate failed'
              : 'delegate response',
          })
          // The response bubble belongs to the DELEGATE agent — whatever
          // follows (the delegating agent's synthesis echo) starts fresh.
          newTurnNext = true
        }
      } catch {
        // skip
      }
    }
  }
  // Post-process: mark question blocks as answered if a user message follows
  for (let i = 0; i < displayMsgs.length; i++) {
    const msg = displayMsgs[i]
    if (msg.role !== 'assistant') continue
    const hasQuestion = msg.blocks.some(b => b.type === 'question' && !b.answered)
    if (!hasQuestion) continue
    const hasFollowUp = displayMsgs.slice(i + 1).some(m => m.role === 'user')
    if (hasFollowUp) {
      msg.blocks = msg.blocks.map(b =>
        b.type === 'question' ? { ...b, answered: true } : b
      )
    }
  }
  // Post-process: mark delegate blocks completed. Primary key: task_id
  // (stable + unique) — a delegate_result with a given task_id completes the
  // matching delegate_spawn block. Fallback for any event/block missing a
  // task_id: count spawns vs results per task_name. The status-bar badge
  // derives from these blocks (no parallel array).
  const completedTaskIds = new Set<string>()
  const statusByTaskId: Record<string, string> = {}
  const spawnCounts: Record<string, number> = {}
  const resultCounts: Record<string, number> = {}
  for (const m of dbMessages) {
    if (m.role !== 'event' || !m.event_data) continue
    try {
      const ed = JSON.parse(m.event_data)
      const name = ed.task_name || ''
      if (m.event_type === 'delegate_spawn') {
        spawnCounts[name] = (spawnCounts[name] || 0) + 1
      } else if (m.event_type === 'delegate_result') {
        resultCounts[name] = (resultCounts[name] || 0) + 1
        if (ed.task_id) {
          completedTaskIds.add(ed.task_id)
          statusByTaskId[ed.task_id] = ed.status || 'completed'
        }
      }
    } catch { /* skip */ }
  }
  // Walk blocks in REVERSE — last N (spawns-results) per name are running
  const runningLeft: Record<string, number> = {}
  for (const name of Object.keys(spawnCounts)) {
    runningLeft[name] = Math.max(0, spawnCounts[name] - (resultCounts[name] || 0))
  }
  for (let i = displayMsgs.length - 1; i >= 0; i--) {
    const msg = displayMsgs[i]
    for (let j = msg.blocks.length - 1; j >= 0; j--) {
      const b = msg.blocks[j]
      if (b.type !== 'delegate') continue
      if (b._taskId) {
        // Resolve the terminal status (completed/failed/cancelled) from the
        // matching delegate_result; still 'running' until its result arrives.
        const st = completedTaskIds.has(b._taskId)
          ? ((statusByTaskId[b._taskId] || 'completed') as 'completed' | 'failed' | 'cancelled' | 'user_interrupted')
          : ('running' as const)
        msg.blocks[j] = { ...b, status: st }
      } else {
        const name = b.taskName
        const left = runningLeft[name] || 0
        if (left > 0) {
          msg.blocks[j] = { ...b, status: 'running' as const }
          runningLeft[name] = left - 1
        } else {
          msg.blocks[j] = { ...b, status: 'completed' as const }
        }
      }
    }
  }
  // Post-process: intra-message document_preview dedupe — within ONE message
  // keep only the LAST block per fileId (interactive chats persist every
  // intra-turn push; the pump path already dedupes per turn). Cross-message
  // instances are KEPT: previewChainModes renders them as the live preview,
  // the view-only "previous version", or a chip.
  for (const dm of displayMsgs) {
    const lastIdxByFile = new Map<string, number>()
    dm.blocks.forEach((b, bi) => {
      if (b.type === 'document_preview') lastIdxByFile.set(b.fileId, bi)
    })
    if (lastIdxByFile.size) {
      dm.blocks = dm.blocks.filter((b, bi) =>
        b.type !== 'document_preview' || lastIdxByFile.get(b.fileId) === bi,
      )
    }
  }
  return displayMsgs
}

export type PreviewChainMode = 'live' | 'frozen' | 'chip'

/**
 * Render-time live → frozen → chip chain per fileId over the combined loaded
 * block list: a file's LAST preview occurrence is the live block, the one
 * before it the view-only "previous version" (frozen to its own push-time
 * snapshot), anything older a chip. Keys are `${msgIdx}:${blockIdx}`.
 *
 * Computed at render (like supersededUiBlocks) so live streaming, history
 * reload, and scroll-back pagination all agree — never positionally ("all but
 * last message") and never only at rebuild. That is what fixes the deferred
 * collapse landing on the live block after an interleaved text turn, and the
 * loadOlder duplicate-live-block hole (an older page can only add frozen/chip
 * entries — the true latest instance is always already loaded).
 */
export function previewChainModes(
  messages: DisplayMessage[],
): Map<string, PreviewChainMode> {
  const perFile = new Map<string, string[]>()
  messages.forEach((msg, mi) =>
    msg.blocks.forEach((b, bi) => {
      if (b.type === 'document_preview') {
        const keys = perFile.get(b.fileId) ?? []
        keys.push(`${mi}:${bi}`)
        perFile.set(b.fileId, keys)
      }
    }),
  )
  const out = new Map<string, PreviewChainMode>()
  for (const keys of perFile.values()) {
    keys.forEach((key, i) => {
      out.set(
        key,
        i === keys.length - 1 ? 'live' : i === keys.length - 2 ? 'frozen' : 'chip',
      )
    })
  }
  return out
}

/** Data a bgcommand pill borrows from the Bash tool block it spawned from. */
export interface BgCommandPair {
  toolInput?: any
  toolResult?: string
  resultSummary?: string
}

/**
 * Pair each background-command block with the Bash tool block it spawned from
 * (same tool_use_id) inside ONE message's block list. The tool pill is hidden
 * and its input/result render inside the (expandable) bgcommand pill — one
 * pill per background command instead of two stacked ones. Unpaired blocks
 * (Codex null ids, split messages, old rows) render as before.
 *
 * Pure — shared by the live stream, DB reload, and reconnect paths, and unit
 * tested directly.
 */
export function pairBgCommandBlocks(blocks: MessageBlock[]): {
  hiddenToolIdx: Set<number>
  bgPairs: Map<number, BgCommandPair>
} {
  const hiddenToolIdx = new Set<number>()
  const bgPairs = new Map<number, BgCommandPair>()
  let toolIdxById: Map<string, number> | null = null
  for (let i = 0; i < blocks.length; i++) {
    const b = blocks[i]
    if (b.type !== 'bgcommand' || !b._toolId) continue
    if (toolIdxById === null) {
      toolIdxById = new Map()
      for (let j = 0; j < blocks.length; j++) {
        const t = blocks[j]
        if (t.type === 'tool' && t.toolId) toolIdxById.set(t.toolId, j)
      }
    }
    const ti = toolIdxById.get(b._toolId)
    if (ti === undefined) continue
    const t = blocks[ti]
    if (t.type !== 'tool') continue
    hiddenToolIdx.add(ti)
    bgPairs.set(i, {
      toolInput: t.toolInput,
      toolResult: t.toolResult,
      resultSummary: t.resultSummary,
    })
  }
  return { hiddenToolIdx, bgPairs }
}

/**
 * Keys ("msgIdx:blockIdx") of `ui` artifact blocks superseded by a LATER
 * block showing the SAME workspace file — a re-displayed artifact renders
 * only at its newest chat position; older copies collapse to a chip (and
 * stop paying iframe/live-reload cost). Path identity is the same key the
 * live-reload broadcast matches on; blocks without a path never supersede.
 *
 * Pure — computed at render over whatever is loaded, so live streaming,
 * history reload, and scroll-back pagination agree (older pages prepend:
 * the true latest instance is always already loaded, so a load can only
 * add superseded entries, never flip the latest).
 */
export function supersededUiBlocks(messages: DisplayMessage[]): Set<string> {
  const lastByPath = new Map<string, string>()
  const seen: Array<[string, string]> = []
  messages.forEach((msg, mi) =>
    msg.blocks.forEach((b, bi) => {
      if (b.type === 'ui' && b.path) {
        const key = `${mi}:${bi}`
        seen.push([b.path, key])
        lastByPath.set(b.path, key)
      }
    }),
  )
  const out = new Set<string>()
  for (const [path, key] of seen) {
    if (lastByPath.get(path) !== key) out.add(key)
  }
  return out
}

/**
 * Latest non-empty title per artifact path. An html-less re-display carries
 * no title, so its block (and the superseded chips) inherit the name the
 * artifact was created with.
 */
export function uiTitlesByPath(messages: DisplayMessage[]): Map<string, string> {
  const titles = new Map<string, string>()
  for (const msg of messages) {
    for (const b of msg.blocks) {
      if (b.type === 'ui' && b.path && b.title) titles.set(b.path, b.title)
    }
  }
  return titles
}
