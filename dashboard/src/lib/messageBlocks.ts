import type { DelegateCardMark, DisplayMessage, MessageBlock } from '../components/chat/types'
import type { PendingFile, PendingImage } from '../store/types'
import { cleanUserMessageText } from './transcriptCleanup'
import { PERSISTED, RESEND_REASONS, SYSTEM_SUBTYPE, WIRE, type DbMessageRow, type LiveBlock, type PersistedEvent } from '../api/wireEvents'
import { DELEGATE_RESULT, RUN_STATUS, type DelegateBlockStatus } from './status/run'

// Pure mappers from wire/DB event shapes to renderable MessageBlocks.
// Single source of truth shared by AgentChat (live chat + task chats)
// (task run view) — both reconstruct history and live-state through these.

/**
 * `cost_billed` off a metadata event/row. Only an explicit `false` (the turn
 * ran on a subscription or a local model) hides the cost; rows persisted
 * before the flag existed carry none and keep showing it.
 */
export function costBilledOf(evt: { cost_billed?: unknown }): boolean | undefined {
  if (evt.cost_billed === false) return false
  return evt.cost_billed === true ? true : undefined
}

/**
 * Whether the chat-level cost gauge shows: the NEWEST persisted turn's flag.
 * The session binding is deleted when a session closes, so the persisted
 * metadata row is the only place the credential kind survives a reload.
 * `true` when no loaded turn carries the flag (fresh chat, pre-flag history).
 */
export function latestCostBilled(dbMessages: DbMessageRow[]): boolean {
  for (let i = dbMessages.length - 1; i >= 0; i--) {
    const m = dbMessages[i]
    if (m?.role !== 'event' || m.event_type !== WIRE.METADATA || !m.event_data) continue
    try {
      return costBilledOf(JSON.parse(m.event_data)) !== false
    } catch {
      return true
    }
  }
  return true
}

/**
 * The tool fees among the loaded rows: an `mcp_cost` row is money whatever
 * the model runs on. Turns from before the fee had a row of its own carry
 * none, so an older chat's reload shows less than its live view did (Usage
 * keeps it all).
 */
export function toolFeesOf(dbMessages: DbMessageRow[]): number {
  let sum = 0
  for (const m of dbMessages) {
    if (m?.role !== 'event' || m.event_type !== WIRE.MCP_COST || !m.event_data) continue
    try {
      const cost = Number(JSON.parse(m.event_data).cost_usd)
      if (cost > 0) sum += cost
    } catch { /* an unreadable row counts nothing */ }
  }
  return sum
}

/**
 * The blocks of a delegate result's own bubble: the worker's report text and
 * the files it attached (`files` / `files_skipped` on the event, agent-relative
 * paths in THIS chat's tree). Empty when the result carries neither, so no
 * bubble is minted for it: a length test, never truthiness (an empty list
 * must not mint an empty card). Shared by the history replay and the live
 * frame, so the two render one shape and the live dedup can compare blocks.
 */
export function delegateResultBlocks(evt: {
  output_text?: string
  files?: Array<{ path: string; bytes: number }>
  files_skipped?: Array<{ path: string; reason: string }>
}): MessageBlock[] {
  const text = evt.output_text || ''
  const files = Array.isArray(evt.files) ? evt.files : []
  const skipped = Array.isArray(evt.files_skipped) ? evt.files_skipped : []
  const blocks: MessageBlock[] = []
  if (text) blocks.push({ type: 'text', content: text })
  if (files.length || skipped.length) blocks.push({ type: 'delegate_files', files, skipped })
  return blocks
}

/**
 * What makes one delegate result's bubble distinct from another's: its text
 * and its files (each `path` and `bytes`), its skipped entries (each `path`
 * and `reason`), and nothing else. One delivery can reach a socket as two
 * frames; the live path renders a bubble once per signature.
 */
export function delegateResultSignature(blocks: MessageBlock[]): string {
  const text = blocks.find((b): b is Extract<MessageBlock, { type: 'text' }> => b.type === 'text')?.content || ''
  const files = blocks.find((b): b is Extract<MessageBlock, { type: 'delegate_files' }> => b.type === 'delegate_files')
  return JSON.stringify([
    text,
    (files?.files || []).map((f) => [f.path, f.bytes]),
    (files?.skipped || []).map((s) => [s.path, s.reason]),
  ])
}

/** A result whose worker's own run cut its turn short before any report. */
export const REPORT_MISSING_BADGE = 'delegate ended without a report'

/** The badge a delegate result's bubble wears, by the run's result word; a
 * result whose worker ended without a report (`report_missing`) says that
 * first. */
export function delegateResultBadge(status: string | undefined, reportMissing = false): string {
  if (reportMissing) return REPORT_MISSING_BADGE
  return status === DELEGATE_RESULT.CANCELLED ? 'delegate canceled'
    : status === DELEGATE_RESULT.FAILED ? 'delegate failed'
    : status === DELEGATE_RESULT.USER_INTERRUPTED ? 'delegate interrupted'
    : 'delegate response'
}

/** The badge the platform stamps on a worker lane's incoming prompt (the
 * stored spelling: the scheduler's prompt row and a lane steer's meta). */
export const DELEGATED_BY_BADGE = 'delegated by'

/** The badge of a meeting participant's bubble. */
export const MEETING_BADGE = 'meeting'

function textHash(text: string): string {
  let h = 5381
  for (let i = 0; i < text.length; i++) h = ((h << 5) + h + text.charCodeAt(i)) | 0
  return (h >>> 0).toString(36)
}

/** A delegate result's card: keyed by its task and its blocks, the same for
 * the live frame and the stored row. */
export function delegateResultCard(taskKey: string | undefined, blocks: MessageBlock[]): DelegateCardMark {
  return { kind: 'result', key: `r:${taskKey || ''}:${textHash(delegateResultSignature(blocks))}` }
}

/** A worker's incoming prompt is a card when the platform stamped it so. */
export function delegatePromptCard(badge: string | undefined, key: string): DelegateCardMark | undefined {
  return badge === DELEGATED_BY_BADGE ? { kind: 'prompt', key } : undefined
}

// The shapes the builders give a delegate card. Any other block (a permission,
// a question, a plan, an artifact) keeps its message uncollapsed.
const CARD_BLOCK_TYPES: ReadonlySet<MessageBlock['type']> = new Set<MessageBlock['type']>([
  'text', 'delegate_files', 'file_attachments', 'image_attachments',
])

/** The card a message renders as, or null. */
export function delegateCardOf(msg: DisplayMessage): DelegateCardMark | null {
  const card = msg.delegateCard
  if (!card) return null
  return msg.blocks.every((b) => CARD_BLOCK_TYPES.has(b.type)) ? card : null
}

// The delegation MCP's two wrappers around a worker's prompt, outer first;
// only a LEADING, CLOSED one is the platform's.
const DELEGATED_WORK_RE = /^\s*\[DELEGATED_WORK\][\s\S]*?\[\/DELEGATED_WORK\]\s*/
const TASK_OUTPUT_DIR_RE = /^\s*\[TASK_OUTPUT_DIR\][\s\S]*?\[\/TASK_OUTPUT_DIR\]\s*/
// The bounded result's own head line (lanes._delegate_output_preview).
const OUTPUT_TRUNCATED_RE = /^\s*… \[output truncated[^\]\n]*\]\s*/
const MD_LINE_LEAD_RE = /^\s*(?:#{1,6}\s+|>\s*|[-*+]\s+|\d+\.\s+)/
// A report's tail that is too short to stand alone ("Blocked on: none").
const SHORT_TAIL = 80

function resultTail(text: string): string {
  const paras = text.replace(OUTPUT_TRUNCATED_RE, '').split(/\n\s*\n/).map((p) => p.trim()).filter(Boolean)
  const last = paras[paras.length - 1] ?? ''
  return last.length < SHORT_TAIL && paras.length > 1 ? `${paras[paras.length - 2]}\n${last}` : last
}

// What a card's preview shows of its text, by its kind.
const PREVIEW_BODY: Record<DelegateCardMark['kind'], (text: string) => string> = {
  prompt: (text) => text.replace(DELEGATED_WORK_RE, '').replace(TASK_OUTPUT_DIR_RE, ''),
  result: resultTail,
}

/**
 * The two-line preview of a delegate card: a prompt's task (its head, the
 * platform's wrappers stripped) or a result's outcome (its tail: a lane's
 * report ends its narration). Plain text: the leading markdown marks of
 * each line, emphasis stars and backticks dropped. The stored text and the
 * expanded card are untouched; a preview the stripping would empty shows
 * the text as it is.
 */
export function delegatePreviewText(kind: DelegateCardMark['kind'], blocks: MessageBlock[]): string {
  const text = blocks
    .filter((b): b is Extract<MessageBlock, { type: 'text' }> => b.type === WIRE.TEXT)
    .map((b) => b.content)
    .join('\n\n')
  let body = PREVIEW_BODY[kind](text)
  if (!body.trim()) body = text
  return body
    .split('\n')
    .map((line) => line.replace(MD_LINE_LEAD_RE, '').replace(/\*+|`+/g, ''))
    .join(' ')
    .replace(/\s+/g, ' ')
    .trim()
}

type SystemBlock = Extract<MessageBlock, { type: 'system' }>

/** The block a review's bubble starts with: how many of the agent's
 *  background agents or commands finished (the nudge row's `count`, the
 *  live frame's). */
export function nudgeBlock(subtype: string, count: unknown): SystemBlock {
  return typeof count === 'number' && count > 0
    ? { type: 'system', subtype, count }
    : { type: 'system', subtype }
}

/** The faint line a nudge block reads as ("14 background commands
 *  finished"), null for any other block. */
export function nudgeNote(b: MessageBlock): string | null {
  if (b.type !== WIRE.SYSTEM) return null
  const agents = b.subtype === SYSTEM_SUBTYPE.BG_AGENTS_COMPLETED
  if (!agents && b.subtype !== SYSTEM_SUBTYPE.BG_COMMANDS_COMPLETED) return null
  const kind = agents ? 'agent' : 'command'
  const n = b.count ?? 0
  return n > 0 ? `${n} background ${kind}${n === 1 ? '' : 's'} finished` : `Background ${kind}s finished`
}

/** Whether `next` may go on from `above`: the agent's own reply (not a
 *  person's message, not a delegate's card) and the same speaker (a meeting
 *  turn or another agent keeps its header). */
export function continuesAbove(above: DisplayMessage | undefined, next: DisplayMessage): boolean {
  return !!above && above.role === 'assistant' && next.role === 'assistant'
    && !above.delegateCard && !next.delegateCard
    && (above.agentSlug || '') === (next.agentSlug || '')
}

/** The identity a background review's bubble takes from the bubble above
 *  it: a meeting participant's (the review speaks in the meeting), else
 *  none (the page's agent). The history and the live stream both ask. */
export function reviewSpeaker(above: DisplayMessage | undefined): Partial<DisplayMessage> {
  return above?.role === 'assistant' && above.badge === MEETING_BADGE && above.agentSlug
    ? { agentSlug: above.agentSlug, agentDisplayName: above.agentDisplayName, agentColor: above.agentColor, badge: above.badge }
    : {}
}

/** Convert a live_state inline_block to a MessageBlock for reconnect rendering. */
export function liveBlockToMessageBlock(ib: LiveBlock): MessageBlock | null {
  switch (ib.type) {
    case WIRE.IMAGES:
      return {
        type: 'images',
        images: (Array.isArray(ib.images) ? ib.images : []).map((it) => ({
          url: it.url || undefined,
          imageData: it.image_data || undefined,
          mimeType: it.mime_type || undefined,
          caption: it.caption || undefined,
          attribution: it.attribution || undefined,
          linkUrl: it.link_url || undefined,
          downloadUrl: it.download_url || undefined,
        })),
      }
    case WIRE.IMAGE_GENERATING:
      return { type: 'image_generating', promptPreview: ib.prompt_preview || '', model: ib.model || '' }
    case WIRE.URL:
      return { type: 'url', url: ib.url, title: ib.title, description: ib.description || '' }
    case WIRE.FILE:
      return { type: 'file', filename: ib.filename, downloadUrl: ib.download_url, description: ib.description || '' }
    case WIRE.VIDEO:
      return { type: 'video', srcKind: ib.src_kind === 'token' ? 'token' : 'url', url: ib.url || undefined, mediaUrl: ib.media_url || undefined, token: ib.token || undefined, mime: ib.mime || undefined, caption: ib.caption || undefined, title: ib.title || undefined, poster: ib.poster || undefined }
    case WIRE.AUDIO:
      return { type: 'audio', srcKind: ib.src_kind === 'token' ? 'token' : 'url', url: ib.url || undefined, mediaUrl: ib.media_url || undefined, token: ib.token || undefined, mime: ib.mime || undefined, caption: ib.caption || undefined, title: ib.title || undefined }
    case WIRE.MEDIA_PROCESSING:
      return { type: 'media_processing', mediaKind: ib.media_kind === 'audio' ? 'audio' : 'video', caption: ib.caption || undefined }
    case WIRE.DOCUMENT_PREVIEW:
      return { type: 'document_preview', filename: ib.filename, fileId: ib.file_id, downloadUrl: ib.download_url, snapshotId: ib.snapshot_id || undefined, generation: ib.generation || undefined, version: ib.version || undefined }
    case WIRE.UI:
      return { type: 'ui', token: ib.token || '', uiUrl: ib.ui_url || '', title: ib.title || undefined, height: typeof ib.height === 'number' ? ib.height : undefined, path: ib.path || undefined }
    case WIRE.ARTIFACT_INTERACTION:
      return { type: 'artifact_interaction', token: ib.token || '', title: ib.title || undefined, payload: ib.payload }
    case WIRE.APP_ACTION:
      return { type: 'app_action', appId: ib.app_id || '', slug: ib.slug || undefined, title: ib.title || undefined, actionId: ib.action_id || '', label: ib.label || undefined, prompt: ib.prompt || undefined }
    case WIRE.QUESTION:
      return { type: 'question', toolName: ib.tool_name || '', toolInput: ib.tool_input || {}, answered: false }
    case WIRE.THINKING:
      return { type: 'thinking', content: ib.content || '', collapsed: true, done: true }
    case WIRE.METADATA:
      return { type: 'metadata', costUsd: ib.cost_usd ?? 0, durationMs: ib.duration_ms ?? 0, costBilled: costBilledOf(ib) }
    case WIRE.PLAN_MODE:
      return { type: 'plan', action: ib.action || 'enter', toolInput: ib.tool_input }
    case WIRE.SYSTEM:
      return { type: 'system', subtype: ib.subtype || '', message: ib.message, reason: ib.reason, agentName: ib.agent_display_name || ib.agent, agentColor: ib.agent_color }
    case WIRE.CHECK_VERDICT:
      // The pump keeps the verdict card event in the live state as-is.
      return eventToBlock(ib)
    case WIRE.CONTEXT_COMPACT:
      return { type: 'system', subtype: SYSTEM_SUBTYPE.CONTEXT_COMPRESSED }
    default:
      return null
  }
}

export function eventToBlock(evt: PersistedEvent, dbMessageId?: number): MessageBlock | null {
  switch (evt.type) {
    case WIRE.IMAGES:
      return {
        type: 'images',
        images: (Array.isArray(evt.images) ? evt.images : []).map((it) => ({
          url: it.url || undefined,
          imageData: it.image_data || undefined,
          mimeType: it.mime_type || undefined,
          caption: it.caption || undefined,
          attribution: it.attribution || undefined,
          linkUrl: it.link_url || undefined,
          downloadUrl: it.download_url || undefined,
        })),
      }
    case WIRE.URL:
      return { type: 'url', url: evt.url, title: evt.title, description: evt.description || '' }
    case WIRE.FILE:
      return { type: 'file', filename: evt.filename, downloadUrl: evt.download_url, description: evt.description || '' }
    case WIRE.VIDEO:
      return { type: 'video', srcKind: evt.src_kind === 'token' ? 'token' : 'url', url: evt.url || undefined, mediaUrl: evt.media_url || undefined, token: evt.token || undefined, mime: evt.mime || undefined, caption: evt.caption || undefined, title: evt.title || undefined, poster: evt.poster || undefined }
    case WIRE.AUDIO:
      return { type: 'audio', srcKind: evt.src_kind === 'token' ? 'token' : 'url', url: evt.url || undefined, mediaUrl: evt.media_url || undefined, token: evt.token || undefined, mime: evt.mime || undefined, caption: evt.caption || undefined, title: evt.title || undefined }
    case WIRE.DOCUMENT_PREVIEW:
      return { type: 'document_preview', filename: evt.filename, fileId: evt.file_id, downloadUrl: evt.download_url, dbMessageId, snapshotId: evt.snapshot_id || undefined, generation: evt.generation || undefined, version: evt.version || undefined }
    case WIRE.UI:
      return { type: 'ui', token: evt.token || '', uiUrl: evt.ui_url || '', title: evt.title || undefined, height: typeof evt.height === 'number' ? evt.height : undefined, path: evt.path || undefined }
    case WIRE.ARTIFACT_INTERACTION:
      return { type: 'artifact_interaction', token: evt.token || '', title: evt.title || undefined, payload: evt.payload }
    case WIRE.APP_ACTION:
      return { type: 'app_action', appId: evt.app_id || '', slug: evt.slug || undefined, title: evt.title || undefined, actionId: evt.action_id || '', label: evt.label || undefined, prompt: evt.prompt || undefined }
    case PERSISTED.TOOL:
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
    case WIRE.TASK_SPAWN:
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
    case WIRE.BG_COMMAND_SPAWN:
      return {
        type: 'bgcommand',
        command: evt.command || '',
        description: evt.description || '',
        isActive: false,  // Historical = finished (spawn-only persist, mirrors subagents)
        _toolId: evt.tool_use_id || undefined,
      }
    case WIRE.DELEGATE_SPAWN:
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
    case WIRE.DELEGATE_RESULT:
      return null  // Handled by post-processing (marks matching delegate_spawn as completed)
    case PERSISTED.SCHEDULE_WAKE:
      // A scheduled self-continuation drove this turn, or the platform
      // resumed the chat after the agent moved it (`source` move): the row.
      return { type: 'schedulewake', prompt: evt.prompt || '', ...(evt.source ? { source: evt.source } : {}) }
    case WIRE.CHECK_VERDICT:
      // A check judged this turn (CHECKS.md): the compact verdict card.
      return {
        type: 'checkverdict', check: evt.check || '', status: evt.status || 'error',
        pass: !!evt.pass, score: evt.score ?? null, summary: evt.summary || '',
        findings: Array.isArray(evt.findings) ? evt.findings : [],
        findingsTotal: typeof evt.findings_total === 'number' ? evt.findings_total : (Array.isArray(evt.findings) ? evt.findings.length : 0),
        round: evt.round || 1, rounds: evt.rounds || 0, ranOn: evt.ran_on || '',
        costUsd: typeof evt.cost_usd === 'number' ? evt.cost_usd : 0, verdictId: evt.verdict_id || undefined,
      }
    case WIRE.THINKING:
      return { type: 'thinking', content: evt.content || '', collapsed: true, done: true }
    case WIRE.PERMISSION_PROMPT:
      return {
        type: 'permission',
        requestId: evt.request_id || '',
        toolName: evt.tool_name || '',
        toolInput: evt.tool_input || {},
        description: evt.description,
        resolved: true,
        approved: evt.approved !== false,  // Default true unless explicitly rejected (dead session)
      }
    case WIRE.QUESTION:
      return { type: 'question', toolName: evt.tool_name || '', toolInput: evt.tool_input || {}, answered: false }
    case WIRE.PLAN_MODE:
      return { type: 'plan', action: evt.action || 'enter', toolInput: evt.tool_input }
    case WIRE.PLAN_REVIEW:
      return {
        type: 'plan_review',
        requestId: evt.request_id || '',
        plan: evt.plan || '',
        toolInput: evt.tool_input || {},
        filename: evt.filename || '',
        resolved: true,  // Historical = already resolved
        action: evt.action || 'reject',  // No action saved = was never resolved = cancelled
        ...(evt.action ? {} : { unanswered: true }),  // its plan row may say more (withPlanRows)
      }
    case WIRE.SYSTEM:
      return { type: 'system', subtype: evt.subtype || '', message: evt.message, reason: evt.reason, agentName: evt.agent_display_name || evt.agent, agentColor: evt.agent_color }
    case WIRE.METADATA:
      return { type: 'metadata', costUsd: evt.cost_usd ?? 0, durationMs: evt.duration_ms ?? evt.duration_api_ms ?? 0, costBilled: costBilledOf(evt) }
    case PERSISTED.BG_NUDGE:
      return nudgeBlock(SYSTEM_SUBTYPE.BG_AGENTS_COMPLETED, evt.count)
    case PERSISTED.BG_COMMAND_NUDGE:
      return nudgeBlock(SYSTEM_SUBTYPE.BG_COMMANDS_COMPLETED, evt.count)
    case WIRE.CONTEXT_COMPACT:
      // The pump persists the compaction's `completed` block; the live path
      // shows the same separator (core-seams phase 6, D9b).
      return { type: 'system', subtype: SYSTEM_SUBTYPE.CONTEXT_COMPRESSED }
    case WIRE.FG_AGENTS_COMPLETE:
    case WIRE.BG_AGENT_DONE:
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
/**
 * The attachment blocks of a user message from the server's meta — the
 * `event_data` of a stored row and the `images` / `files` of a `queued`,
 * `steered` or `queue_sent` frame are the same shape, so a bubble rendered
 * live looks like the same bubble after a reload.
 */
export function attachmentBlocks(
  images?: Array<{ name: string; path?: string }> | null,
  files?: Array<{ name: string; path?: string; folder?: boolean }> | null,
): MessageBlock[] {
  const blocks: MessageBlock[] = []
  if (Array.isArray(files) && files.length) {
    blocks.push({
      type: 'file_attachments',
      files: files.map(f => ({
        name: f.name, path: f.path, ...(f.folder === true ? { folder: true } : {}),
      })),
    })
  }
  if (Array.isArray(images) && images.length) {
    blocks.push({
      type: 'image_attachments',
      images: images.map(i => i.name),
      // saved upload paths — rows persisted before paths were stored
      // carry none, and the renderer falls back to the count badge
      paths: images.map(i => i.path ?? null),
    })
  }
  return blocks
}

/** A bubble whose turn is over: it carries the turn's closing metadata, or the
 * card of a turn that ended short. */
export function finishedBubble(m: DisplayMessage): boolean {
  return m.blocks.some((b) => b.type === WIRE.METADATA
    || (b.type === WIRE.SYSTEM && b.subtype === SYSTEM_SUBTYPE.TURN_ENDED))
}

/**
 * The ids of the bubbles of the turn still open: walking back from the newest
 * message over the assistant bubbles not finished yet (a message steered into
 * this turn is passed over), up to the first finished one or the message
 * that started the turn (an earlier bubble that ended without its metadata,
 * an error, is not this turn's). None while the chat is not
 * streaming, nor in the moment a server turn starts before its first frame
 * (the newest bubble is the last turn's, finished). Their steps chips count
 * a waited sub-agent or background command as running, and the newest says
 * "Working…" (lib/activityGroups).
 */
export function openTurnBubbles(messages: DisplayMessage[], streaming: boolean): Set<string> {
  const open = new Set<string>()
  if (!streaming) return open
  for (let i = messages.length - 1; i >= 0; i--) {
    const m = messages[i]
    if (m.role !== 'assistant') {
      if (m.steered) continue
      break
    }
    if (m.delegateCard || finishedBubble(m)) break
    open.add(m.id)
  }
  return open
}

export function dbMessagesToDisplay(
  dbMessages: DbMessageRow[],
  agents: Array<{ name: string; display_name?: string; color?: string }> | undefined,
): DisplayMessage[] {
  const displayMsgs: DisplayMessage[] = []
  // Current-name-wins: a renamed agent shows its new name on historical rows;
  // the frozen event_data name is the fallback so deleted agents keep a label.
  const currentName = (slug?: string) =>
    slug ? agents?.find(a => a.name === slug)?.display_name : undefined
  // Events that signal a new LLM turn (delegate result delivery, bg agent nudge)
  let newTurnNext = false
  // The boundary armed is a background review's: its bubble goes on from
  // the agent's bubble above (`continuation`).
  let continueNext = false
  for (const m of dbMessages) {
    if (m.role === 'user') {
      const blocks: MessageBlock[] = []
      let agentMeta: { agent_slug?: string; agent_display_name?: string; agent_color?: string; badge?: string } = {}
      // Check for image attachments and agent identity in event_data
      if (m.event_data) {
        try {
          const ed = JSON.parse(m.event_data)
          blocks.push(...attachmentBlocks(ed.images, ed.files))
          if (ed.agent_slug) agentMeta = ed
        } catch { /* ignore */ }
      }
      // Render-side cleanup (data untouched): injected time preludes strip off
      // the bubble; pure slash-command records hide the whole row — unless it
      // carries attachments, which keep the row alive without the noise text.
      const cleanedText = cleanUserMessageText(m.content)
      if (cleanedText === null && blocks.length === 0) continue
      if (cleanedText !== null) blocks.push({ type: 'text', content: cleanedText })
      const promptCard = agentMeta.agent_slug ? delegatePromptCard(agentMeta.badge, `db-${m.id}`) : undefined
      displayMsgs.push({
        id: `db-${m.id}`,
        role: 'user',
        blocks,
        createdAt: m.created_at,
        ...(agentMeta.agent_slug ? {
          agentSlug: agentMeta.agent_slug,
          agentDisplayName: currentName(agentMeta.agent_slug) ?? agentMeta.agent_display_name,
          agentColor: agentMeta.agent_color,
          badge: agentMeta.badge,
        } : {}),
        ...(promptCard ? { delegateCard: promptCard } : {}),
      })
      newTurnNext = false
      continueNext = false
    } else if (m.role === 'assistant') {
      // Merge with preceding assistant message (same turn, split by events)
      // BUT start a new message if a turn-boundary event preceded this
      const last = displayMsgs[displayMsgs.length - 1]
      // Check for agent identity in event_data (meeting messages)
      let msgAgentSlug: string | undefined
      let msgAgentDisplayName: string | undefined
      let msgAgentColor: string | undefined
      let msgBadge: string | undefined
      let note = false
      if (m.event_data) {
        try {
          const ed = JSON.parse(m.event_data)
          msgAgentSlug = ed.agent_slug
          msgAgentDisplayName = ed.agent_display_name
          msgAgentColor = ed.agent_color
          msgBadge = ed.badge
          note = ed.note === true
        } catch { /* ignore */ }
      }
      // A progress note's row (`event_data.note`): the same text, drawn faint.
      const content = m.content || ''
      const textBlock: MessageBlock = note ? { type: 'text', content, note: true } : { type: 'text', content }
      // Force new message if: turn boundary, or different agent identity
      const forceNew = newTurnNext || (
        msgAgentSlug && last?.role === 'assistant' && msgAgentSlug !== last.agentSlug
      )
      if (last && last.role === 'assistant' && !forceNew) {
        if (m.content) last.blocks.push(textBlock)
      } else {
        const next: DisplayMessage = {
          id: `db-${m.id}`,
          role: 'assistant',
          blocks: m.content ? [textBlock] : [],
          createdAt: m.created_at,
          agentSlug: msgAgentSlug,
          agentDisplayName: currentName(msgAgentSlug) ?? msgAgentDisplayName,
          agentColor: msgAgentColor,
          badge: msgBadge,
        }
        if (continueNext && continuesAbove(last, next)) next.continuation = true
        displayMsgs.push(next)
      }
      newTurnNext = false
      continueNext = false
    } else if (m.role === 'event' && m.event_data) {
      // delegate_result / bg_nudge / artifact_interaction / app_action /
      // schedule_wake signal a new LLM turn (a wake's marker opens the bubble
      // its answer lands in, a nudge's line the review's)
      const nudge = m.event_type === PERSISTED.BG_NUDGE || m.event_type === PERSISTED.BG_COMMAND_NUDGE
      if (m.event_type === WIRE.DELEGATE_RESULT || nudge || m.event_type === WIRE.ARTIFACT_INTERACTION || m.event_type === WIRE.APP_ACTION || m.event_type === PERSISTED.SCHEDULE_WAKE) {
        newTurnNext = true
        continueNext = nudge
      }
      // Meeting turn start: force new message with agent identity
      let meetingTurnAgent: { slug: string; displayName: string; color: string } | null = null
      if (m.event_type === WIRE.SYSTEM) {
        try {
          const sysEd = JSON.parse(m.event_data || '{}')
          if (sysEd.subtype === SYSTEM_SUBTYPE.MEETING_TURN_START) {
            newTurnNext = true
            continueNext = false
            meetingTurnAgent = {
              slug: sysEd.agent || '',
              displayName: currentName(sysEd.agent) ?? (sysEd.agent_display_name || sysEd.agent || ''),
              color: sysEd.agent_color || '',
            }
          }
        } catch { /* ignore */ }
      }
      try {
        const evt = JSON.parse(m.event_data)
        // Skip dismissed events
        if (evt.dismissed) continue
        // A wake's or a nudge's row data carries no type (its column names it).
        if (!evt.type && (m.event_type === PERSISTED.SCHEDULE_WAKE || nudge)) evt.type = m.event_type
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
          // A second kind of the same review (agents, then commands): its
          // line joins the first one's bubble, which holds nudge lines only.
          const prior = displayMsgs[displayMsgs.length - 1]
          if (nudge && prior?.role === 'assistant' && prior.blocks.length > 0
              && prior.blocks.every((b) => nudgeNote(b) !== null)) {
            newTurnNext = false
            continueNext = false
          }
          let lastAssistant = displayMsgs[displayMsgs.length - 1]
          if (newTurnNext || !lastAssistant || lastAssistant.role !== 'assistant') {
            const above = lastAssistant
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
            } else if (continueNext) {
              Object.assign(lastAssistant, reviewSpeaker(above))
            }
            if (continueNext && continuesAbove(above, lastAssistant)) lastAssistant.continuation = true
            displayMsgs.push(lastAssistant)
            newTurnNext = false
            continueNext = false
          }
          if (block) lastAssistant.blocks.push(block)
        }
        // Delegate result with output or attached files: insert as a separate
        // agent message. output_text is non-empty for failed/canceled
        // terminals too (the backend synthesizes a ⚠ marker); a files-only
        // result shows the list alone; neither mints nothing.
        if (m.event_type === WIRE.DELEGATE_RESULT) {
          const blocks = delegateResultBlocks(evt)
          if (blocks.length) {
            const delegateAgent = agents?.find(a => a.name === evt.agent)
            displayMsgs.push({
              id: `db-delresult-${m.id}`,
              role: 'assistant',
              blocks,
              createdAt: m.created_at,
              agentSlug: evt.agent || '',
              agentDisplayName: delegateAgent?.display_name,
              agentColor: delegateAgent?.color || '',
              badge: delegateResultBadge(evt.status, evt.report_missing === true),
              delegateCard: delegateResultCard(evt.task_id || evt.task_name, blocks),
            })
            // The response bubble belongs to the DELEGATE agent — whatever
            // follows (the delegating agent's synthesis echo) starts fresh.
            newTurnNext = true
          }
        }
      } catch {
        // skip
      }
    }
  }
  // Post-process: mark question blocks as answered, and plan-approval
  // blocks as resolved, once a LATER MESSAGE follows them (the user's answer
  // in a headless chat). Later blocks in the SAME assistant message are
  // recorded apart (`followedInTurn`): an interactive terminal's picker
  // answer writes no user row and the agent's continuation lands as sibling
  // blocks, so the renderer counts them for interactive chats only. A
  // headless turn always closes with a metadata block, which would
  // otherwise answer every open question the moment the history is rebuilt.
  for (let i = 0; i < displayMsgs.length; i++) {
    const msg = displayMsgs[i]
    if (msg.role !== 'assistant') continue
    const laterMessage = i < displayMsgs.length - 1
    msg.blocks = msg.blocks.map((b, k) => {
      const laterSibling = k < msg.blocks.length - 1
      if (!laterMessage && !laterSibling) return b
      if (b.type === 'question' && !b.answered) {
        return laterMessage ? { ...b, answered: true } : { ...b, followedInTurn: true }
      }
      if (b.type === 'plan' && b.action === 'exit' && !b.resolved) {
        return laterMessage ? { ...b, resolved: true } : { ...b, followedInTurn: true }
      }
      return b
    })
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
      if (m.event_type === WIRE.DELEGATE_SPAWN) {
        spawnCounts[name] = (spawnCounts[name] || 0) + 1
      } else if (m.event_type === WIRE.DELEGATE_RESULT) {
        resultCounts[name] = (resultCounts[name] || 0) + 1
        if (ed.task_id) {
          completedTaskIds.add(ed.task_id)
          statusByTaskId[ed.task_id] = ed.status || DELEGATE_RESULT.COMPLETED
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
        // Resolve the delegate result from the matching delegate_result
        // row; still running until its result arrives.
        const st: DelegateBlockStatus = completedTaskIds.has(b._taskId)
          ? ((statusByTaskId[b._taskId] || DELEGATE_RESULT.COMPLETED) as DelegateBlockStatus)
          : RUN_STATUS.RUNNING
        msg.blocks[j] = { ...b, status: st }
      } else {
        const name = b.taskName
        const left = runningLeft[name] || 0
        if (left > 0) {
          msg.blocks[j] = { ...b, status: RUN_STATUS.RUNNING }
          runningLeft[name] = left - 1
        } else {
          msg.blocks[j] = { ...b, status: DELEGATE_RESULT.COMPLETED }
        }
      }
    }
  }
  // Post-process: intra-message document_preview dedupe — within ONE message
  // keep only the LAST block per fileId (interactive chats persist every
  // intra-turn push; the pump path already dedupes per turn). Cross-message
  // instances are KEPT: each is a card of its own version.
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

/** A plan review's answers read from its plan row, not a card's button. */
export const PLAN_IMPLEMENTED = 'implemented'
export const PLAN_REVISED = 'revised'

type PlanReviewBlock = Extract<MessageBlock, { type: 'plan_review' }>
const isPlanReview = (b: MessageBlock): b is PlanReviewBlock => b.type === WIRE.PLAN_REVIEW

/** Whether nothing waits on a plan review's card (a Codex plan, answered
 *  between turns). */
export function answeredBetweenTurns(toolInput: unknown): boolean {
  return !!toolInput && typeof toolInput === 'object' && (toolInput as Record<string, unknown>).between_turns === true
}

/**
 * The plan reviews the turn saved unanswered, read from the chat's plan rows
 * (`chat_plans`, the source of truth for a card nobody waits on), so a
 * reload, a second tab and a restart show what the live chat shows. A Codex
 * plan's newest review stays answerable while its row is pending and an
 * older review of the same plan reads as revised; otherwise an implemented
 * row names the answer, and anything else (a rejected row, no row, a Claude
 * review whose turn ended unanswered) stays cancelled.
 */
export function withPlanRows(messages: DisplayMessage[], plans: { filename: string; status: string }[]): DisplayMessage[] {
  const status = new Map(plans.map((p) => [p.filename, p.status]))
  const newest = new Map<string, string>()
  for (const m of messages) {
    for (const b of m.blocks) if (isPlanReview(b) && b.filename) newest.set(b.filename, b.requestId)
  }
  const answer = (b: PlanReviewBlock): MessageBlock => {
    const row = b.filename ? status.get(b.filename) : undefined
    const between = answeredBetweenTurns(b.toolInput)
    if (between && b.filename && newest.get(b.filename) !== b.requestId) return { ...b, action: PLAN_REVISED }
    if (row === PLAN_IMPLEMENTED) return { ...b, action: PLAN_IMPLEMENTED }
    if (between && row !== undefined && row !== 'rejected') return { ...b, resolved: false, action: undefined }
    return b
  }
  return messages.map((m) => (
    m.blocks.some((b) => isPlanReview(b) && b.unanswered)
      ? { ...m, blocks: m.blocks.map((b) => (isPlanReview(b) && b.unanswered ? answer(b) : b)) }
      : m
  ))
}

/** The live chat's earlier reviews of `filename` once a revision arrives
 *  (unanswered, or answered with an edit): read as revised, as a reload
 *  shows them. */
export function reviseOlderPlanReviews(messages: DisplayMessage[], filename: string): DisplayMessage[] {
  const older = (b: MessageBlock) => isPlanReview(b) && b.filename === filename
    && (!b.resolved || b.action === 'edit') && answeredBetweenTurns(b.toolInput)
  if (!filename || !messages.some((m) => m.blocks.some(older))) return messages
  return messages.map((m) => (
    m.blocks.some(older)
      ? { ...m, blocks: m.blocks.map((b) => (older(b) ? { ...b, resolved: true, action: PLAN_REVISED } : b)) }
      : m
  ))
}

/** Data a bgcommand pill borrows from the Bash tool block it spawned from. */
export interface BgCommandPair {
  toolInput?: unknown
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

/** What Resume sends: one short message, since the engine's own record
 *  holds the turn (or the proxy's note of it, after an ending that lost the
 *  session's copy). The proxy knows the sentence (`wire_events.RESUME_TEXT`),
 *  so a second Resume still carries the prompt that started the work. */
export const RESUME_TEXT = 'Continue from where you stopped.'

/** The endings whose card offers Resume once the turn had started. */
const RESUME_REASONS: ReadonlySet<string> = new Set(['error', 'exited', 'silent', 'lost', 'restarted'])
/** The endings that lose the engine's copy of the turn (`TurnEnding.abort_stamps`):
 *  Resume carries the turn's saved attachments again, since the proxy's note
 *  holds text alone (a duplicate is harmless when the session kept them). */
const RESUME_WITH_ATTACHMENTS: ReadonlySet<string> = new Set(['exited', 'silent', 'lost'])
/** Blocks that are not the agent's work: a notice, the closing metadata, a
 *  person's attachments. */
const WORKLESS_BLOCKS: ReadonlySet<string> = new Set([WIRE.SYSTEM, WIRE.METADATA, 'image_attachments', 'file_attachments'])

/** The turn-ended card of a bubble, if it has one. */
export function turnEndedCard(m: DisplayMessage): SystemBlock | undefined {
  return m.blocks.find((b): b is SystemBlock => b.type === WIRE.SYSTEM && b.subtype === SYSTEM_SUBTYPE.TURN_ENDED)
}

/** Whether the turn a card ended had started: an assistant block that is
 *  work (text, a note, a tool, thinking, a plan, a ui) in the span from the
 *  card's bubble back to the message that started the turn, over the
 *  messages steered into it and the assistant bubbles not finished (the
 *  walk of `openTurnBubbles`); an earlier finished bubble or a delegate
 *  card belongs to the previous turn. */
export function turnHadWork(messages: DisplayMessage[], cardMessageId: string): boolean {
  const at = messages.findIndex((m) => m.id === cardMessageId)
  if (at < 0) return false
  for (let i = at; i >= 0; i--) {
    const m = messages[i]
    if (m.role !== 'assistant') {
      if (m.steered) continue
      return false
    }
    if (i < at && (m.delegateCard || finishedBubble(m))) return false
    if (m.blocks.some((b) => !WORKLESS_BLOCKS.has(b.type))) return true
  }
  return false
}

export interface SendAgainAction {
  kind: 'resume' | 'again'
  text: string
  images: PendingImage[]
  files: PendingFile[]
}

/** What the turn-ended card in message `messageId` sends. Resume (one short
 *  message, plus the turn's saved attachments after an ending that lost the
 *  session's copy) when the turn had started and its ending allows it; Send
 *  again (that turn's messages verbatim, an earlier card's included) for a
 *  message that met a stopped engine or a turn that never started; nothing
 *  for a decline, a limit, or a restart that cut nothing. */
export function sendAgainAction(messages: DisplayMessage[], messageId: string): SendAgainAction | null {
  const at = messages.findIndex((m) => m.id === messageId)
  if (at < 0) return null
  const reason = turnEndedCard(messages[at])?.reason
  if (!reason) return null
  const turn = messages.slice(0, at + 1)
  if (RESUME_REASONS.has(reason) && turnHadWork(messages, messageId)) {
    const kept = RESUME_WITH_ATTACHMENTS.has(reason) ? lastTurnPrompt(turn) : null
    return { kind: 'resume', text: RESUME_TEXT, images: kept?.images ?? [], files: kept?.files ?? [] }
  }
  if (!RESEND_REASONS.has(reason)) return null
  const again = lastTurnPrompt(turn)
  return again && { kind: 'again', ...again }
}

/** The card's button for the bubble `m`, or none: the list reads it per
 *  bubble that carries a card. */
export function sendAgainKind(messages: DisplayMessage[], m: DisplayMessage): SendAgainAction['kind'] | undefined {
  if (!turnEndedCard(m)) return undefined
  return sendAgainAction(messages, m.id)?.kind
}

/** What the turn-ended card's Send again sends: the person's messages of
 *  the turn that ended (every user bubble since the last answer text or
 *  the previous turn-ended card: a combined batch, a steer), their texts
 *  joined as the batch was, with their attachments by their saved paths.
 *  Null when there is nothing. */
export function lastTurnPrompt(messages: DisplayMessage[]):
    { text: string; images: PendingImage[]; files: PendingFile[] } | null {
  const textOf = (m: DisplayMessage) =>
    m.blocks.flatMap((b) => (b.type === WIRE.TEXT ? [b.content] : [])).join('\n')
  const endedShort = (m: DisplayMessage) =>
    m.blocks.some((b) => b.type === WIRE.SYSTEM && b.subtype === SYSTEM_SUBTYPE.TURN_ENDED)
  const turn: DisplayMessage[] = []
  for (let i = messages.length - 1; i >= 0; i--) {
    const m = messages[i]
    if (m.role === 'user') turn.unshift(m)
    else if (turn.length && (textOf(m) || endedShort(m))) break
  }
  const text = turn.map(textOf).filter(Boolean).join('\n\n')
  const images: PendingImage[] = []
  const files: PendingFile[] = []
  for (const b of turn.flatMap((m) => m.blocks)) {
    if (b.type === 'image_attachments') {
      b.images.forEach((src: string, i: number) => {
        const path = b.paths?.[i]
        if (path) images.push({ id: `again-img-${path}`, path, name: src })
        // A fresh photo of a live bubble has no saved path yet: its data
        // URL goes again as a new photo.
        else if (src.startsWith('data:')) images.push({ id: `again-img-${i}`, base64: src, name: `photo-${i + 1}` })
      })
    } else if (b.type === 'file_attachments') {
      for (const f of b.files) {
        if (f.path) {
          files.push({
            id: `again-file-${f.path}`, name: f.name, size: 0, uploadedPath: f.path,
            ...(f.folder ? { folder: true } : {}),
          })
        }
      }
    }
  }
  if (!text && !images.length && !files.length) return null
  return { text, images, files }
}

/** An agent's reply, the thing a person may report: never a person's
 *  message, nor another agent's prompt shown as an agent's bubble. */
export function isAgentReply(m: DisplayMessage): boolean {
  return m.role === 'assistant'
}

/** A bubble built from stored rows is named by its first row
 *  (`dbMessagesToDisplay`): `db-<id>`, `db-evt-<id>`, `db-delresult-<id>`. */
const STORED_BUBBLE = /^db-(?:evt-|delresult-)?(\d+)$/

/** The first stored row of a bubble the history built, else null: every
 *  other id is a bubble the page streamed, kept until the chat reloads. */
export function storedRowId(messageId: string): number | null {
  const m = STORED_BUBBLE.exec(messageId)
  return m ? Number(m[1]) : null
}

/** A reply's answer as the person read it, for comparing a streamed bubble
 *  with the same reply built from its rows: the text without the progress
 *  notes, with no spacing (the two join their pieces differently). */
function answerKey(m: DisplayMessage): string {
  return m.blocks
    .flatMap((b) => (b.type === WIRE.TEXT && !b.note ? [b.content] : []))
    .join('')
    .replace(/\s+/g, '')
}

/** A bubble whose reply is whole: built from stored rows, or its turn's
 *  metadata arrived. */
function completeReply(m: DisplayMessage): boolean {
  return storedRowId(m.id) !== null || m.blocks.some((b) => b.type === WIRE.METADATA)
}

/** The first stored row of the streamed reply `messages[index]`, found
 *  among `rows` (the chat's newest, oldest first): the stored reply with the
 *  same answer at the same place from the end among equal answers (a later
 *  equal reply still being written is not stored yet, so it does not count).
 *  Null when it is not there (not stored yet, or it reads differently):
 *  never a guess. */
export function findStoredReply(messages: DisplayMessage[], index: number, rows: DbMessageRow[]): number | null {
  const key = answerKey(messages[index])
  if (!key) return null
  const twinsAfter = messages.slice(index + 1)
    .filter((m) => isAgentReply(m) && completeReply(m) && answerKey(m) === key).length
  const stored = dbMessagesToDisplay(rows, undefined).filter((m) => isAgentReply(m) && answerKey(m) === key)
  const match = stored[stored.length - 1 - twinsAfter]
  return match ? storedRowId(match.id) : null
}
