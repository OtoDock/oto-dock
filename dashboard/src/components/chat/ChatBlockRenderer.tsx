import { type JSX, useLayoutEffect, useRef, useState } from 'react'
import MarkdownContent from './MarkdownContent'
import ImageLightbox, { type LightboxImage } from './media/ImageLightbox'
import ToolActivity from './ToolActivity'
import ThinkingBlock, { NoteBlock } from './ThinkingBlock'
import PermissionDialog from './PermissionDialog'
import SubagentInfo from './SubagentInfo'
import BgCommandInfo from './BgCommandInfo'
import DelegateTaskInfo, { DelegateResultFiles } from './DelegateTaskInfo'
import CheckVerdictCard from './checks/CheckVerdictCard'
import SystemEvent from './SystemEvent'
import ImageGallery from './media/ImageGallery'
import VideoPlayer from './media/VideoPlayer'
import AudioPlayer from './media/AudioPlayer'
import DisplayUrl from './media/DisplayUrl'
import DisplayFile from './media/DisplayFile'
import DocumentCard from './media/DocumentCard'
import UiArtifact from './media/UiArtifact'
import PlanView from './plan/PlanView'
import QuestionDialog from './QuestionDialog'
import SettingCard from './SettingCard'
import ActionCard from './ActionCard'
import ListCard from './ListCard'
import SearchHighlight from './SearchHighlight'

import type { MessageBlock } from './types'
import type { BgCommandPair } from '../../lib/messageBlocks'
import PlanReviewCard from './PlanReviewCard'
import { FILE_ICON_PATH, FOLDER_ICON_PATH } from './attachmentIcons'
import { useSearch } from '../../contexts/useSearch'
import { BG_COMMAND_BLOCK } from '../../lib/kinds/block'
import { CARD } from '../ui/classes'

// URL regex for linkifying plain text (user messages)
const URL_RE = /https?:\/\/[^\s<>)"'\]]+/g

function linkifyText(text: string, matchIdPrefix?: string, matchOrder?: number): (string | JSX.Element)[] {
  const parts: (string | JSX.Element)[] = []
  let last = 0
  let match: RegExpExecArray | null
  let segIdx = 0
  URL_RE.lastIndex = 0
  while ((match = URL_RE.exec(text)) !== null) {
    if (match.index > last) {
      const seg = text.slice(last, match.index)
      if (matchIdPrefix && matchOrder != null) {
        parts.push(<SearchHighlight key={`s${segIdx}`} text={seg} matchId={`${matchIdPrefix}-s${segIdx}`} order={matchOrder} inUserBubble />)
      } else {
        parts.push(seg)
      }
      segIdx++
    }
    const url = match[0]
    parts.push(
      <a
        key={match.index}
        href={url}
        target="_blank"
        rel="noopener noreferrer"
        className="underline underline-offset-2 opacity-90 hover:opacity-100 break-all"
      >
        {url}
      </a>
    )
    last = match.index + url.length
  }
  if (last < text.length) {
    const seg = text.slice(last)
    if (matchIdPrefix && matchOrder != null) {
      parts.push(<SearchHighlight key={`s${segIdx}`} text={seg} matchId={`${matchIdPrefix}-s${segIdx}`} order={matchOrder} inUserBubble />)
    } else {
      parts.push(seg)
    }
  }
  return parts
}

// A person's message folds past FOLD_LINES rendered lines (wrapped lines
// count) to its first FOLDED_LINES.
const FOLD_LINES = 10
const FOLDED_LINES = 8

/** A person's text, folded when long: measured against its own line height
 *  on every change of text or width, under a fade in the bubble's fill, with
 *  Show more at the bottom. Open while a find-in-chat query matches it, so
 *  the match registers and scrolls into view. An unknown line height (a
 *  layout-free test) never folds; an empty text (a photo-only message)
 *  draws nothing. */
function UserMessageText({ text, blockId, blockOrder }: { text: string; blockId: string; blockOrder: number }) {
  const { query } = useSearch()
  const textRef = useRef<HTMLParagraphElement>(null)
  const [open, setOpen] = useState(false)
  // The folded height in px when the text is long enough to fold, else null.
  const [foldPx, setFoldPx] = useState<number | null>(null)
  const needle = query.trim().toLowerCase()
  const expanded = open || (!!needle && text.toLowerCase().includes(needle))

  useLayoutEffect(() => {
    const el = textRef.current
    if (!el) return
    const measure = () => {
      const line = parseFloat(getComputedStyle(el).lineHeight)
      if (!Number.isFinite(line) || line <= 0) {
        setFoldPx(null)
        return
      }
      setFoldPx(el.scrollHeight > FOLD_LINES * line + 1 ? Math.round(FOLDED_LINES * line) : null)
    }
    measure()
    if (typeof ResizeObserver === 'undefined') return
    const ro = new ResizeObserver(measure)
    ro.observe(el)
    return () => ro.disconnect()
  }, [text])

  if (!text) return null
  const folded = foldPx !== null && !expanded
  const toggle = () => {
    // Closing: the bubble's top back in view, not the place the long text
    // left the reader at.
    if (open) textRef.current?.closest('[data-msg-id]')?.scrollIntoView?.({ block: 'nearest' })
    setOpen(!open)
  }
  return (
    <div>
      <div className="relative">
        <p
          ref={textRef}
          className="text-sm whitespace-pre-wrap"
          style={folded ? { maxHeight: foldPx, overflow: 'hidden' } : undefined}
          data-testid="user-message-text"
        >
          {linkifyText(text, query ? blockId : undefined, query ? blockOrder : undefined)}
        </p>
        {folded && (
          <div aria-hidden="true" className="pointer-events-none absolute inset-x-0 bottom-0 h-8 bg-linear-to-t from-action to-transparent" />
        )}
      </div>
      {foldPx !== null && (
        <button
          type="button"
          onClick={toggle}
          aria-expanded={expanded}
          className="mt-1.5 rounded-sm text-xs font-medium underline-offset-2 hover:underline
                     focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-action-fg/60"
        >
          {expanded ? 'Show less' : 'Show more'}
        </button>
      )}
    </div>
  )
}

const PHOTO_ICON_PATH = 'M4 16l4.586-4.586a2 2 0 012.828 0L16 16m-2-2l1.586-1.586a2 2 0 012.828 0L20 14m-6-6h.01M6 20h12a2 2 0 002-2V6a2 2 0 00-2-2H6a2 2 0 00-2 2v12a2 2 0 002 2z'

// A person's photos, each on its own: a fresh one from its data URL, a saved
// one through the agent files API (the route's own role check), and one with
// neither, or whose file does not load (deleted since, or a folder this
// viewer may not read), as a quiet tile instead of a broken image. Opens the
// shared lightbox over the photos that loaded; Chromium blocks window.open
// on data: URLs, so a new-tab navigation would silently do nothing.
function AttachedImageStrip({ srcs }: { srcs: Array<string | null> }) {
  const [lightboxSrc, setLightboxSrc] = useState<string | null>(null)
  const [gone, setGone] = useState<ReadonlySet<number>>(() => new Set())
  const shown = srcs.flatMap((src, i) => (src && !gone.has(i) ? [src] : []))
  return (
    <div className="flex gap-2 flex-wrap mb-1">
      {srcs.map((src, i) => (src && !gone.has(i) ? (
        <img
          key={i}
          src={src}
          alt={`Attached image ${i + 1}`}
          className="w-20 h-20 rounded-card object-cover border border-action-fg/30 cursor-pointer
                     hover:opacity-90 transition-opacity"
          onClick={() => setLightboxSrc(src)}
          onError={() => setGone((prev) => new Set(prev).add(i))}
        />
      ) : (
        <div
          key={i}
          data-testid="photo-not-available"
          className="w-20 h-20 rounded-card border border-dashed border-action-fg/40 flex flex-col items-center
                     justify-center gap-1 px-1 text-center text-2xs leading-tight text-action-fg/80"
        >
          <svg className="w-4 h-4 shrink-0" fill="none" stroke="currentColor" viewBox="0 0 24 24" aria-hidden="true">
            <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d={PHOTO_ICON_PATH} />
          </svg>
          Photo not available
        </div>
      )))}
      {lightboxSrc !== null && shown.includes(lightboxSrc) && (
        <ImageLightbox
          images={shown.map((src): LightboxImage => {
            const m = src.match(/^data:([^;,]+);base64,(.*)$/)
            return m ? { imageData: m[2], mimeType: m[1] } : { url: src }
          })}
          initialIndex={shown.indexOf(lightboxSrc)}
          onClose={() => setLightboxSrc(null)}
        />
      )}
    </div>
  )
}

export default function BlockRenderer({
  block,
  blockId,
  blockOrder,
  isUserMessage,
  chatId,
  onPermissionRespond,
  onPlanReviewResponse,
  onImplementPlan,
  onImplementPlanCodex,
  onQuestionAnswer,
  onQuestionAnswerStructured,
  onSendMessage,
  onSendAgain,
  sendAgainKind,
  onPlanFetched,
  onArtifactInteraction,
  bgPair,
  agentName,
  uiSuperseded,
  uiTitle,
  dialogsInTerminal,
  noteAside,
}: {
  block: MessageBlock
  blockId: string
  blockOrder: number            // explicit sort key for search match ordering
  isUserMessage: boolean
  chatId?: string
  /** Chat's agent slug — past image_attachments render via its files API. */
  agentName?: string
  onPermissionRespond: (requestId: string, approved: boolean) => void
  onPlanReviewResponse?: (requestId: string, action: string) => void
  onImplementPlan?: (planPath: string, mode: string) => void
  onImplementPlanCodex?: (mode: string) => void
  onQuestionAnswer?: (response: string) => void
  onQuestionAnswerStructured?: (requestId: string, answers: Record<string, { answers: string[] }>) => void
  onSendMessage?: (text: string) => void
  /** The turn-ended card's button: the page sends it (Resume, or the last
   *  prompt again), the list says which from the transcript. */
  onSendAgain?: () => void
  sendAgainKind?: 'resume' | 'again'
  onPlanFetched?: (filename: string, content: string) => void
  /** A live interactive terminal owns this chat's dialogs: question and plan
   * cards render read-only — the answer / approval happens in the TUI, and a
   * card answer would only be held until the parked turn ends. */
  dialogsInTerminal?: boolean
  /** display_ui backchannel sender — absent on read-only surfaces (history,
      task runs), where the artifact acks `unavailable` instead. */
  onArtifactInteraction?: (token: string, title: string, payload: unknown) => Promise<{ status: string; reason?: string }>
  /** bgcommand blocks: input/result borrowed from the paired (hidden) Bash
   * tool block — see pairBgCommandBlocks. */
  bgPair?: BgCommandPair
  /** ui blocks: a LATER block re-shows the same artifact file — render a
   * compact reference chip instead of a second full instance. */
  uiSuperseded?: boolean
  /** ui blocks: fallback title inherited from an earlier display of the same
   * path (html-less re-displays carry none on the wire). */
  uiTitle?: string
  /** A progress note drawn as an aside (faint, `noteAsides`); a note that is
   * a real message is drawn as text. */
  noteAside?: boolean
}) {
  const { query: searchQuery } = useSearch()
  switch (block.type) {
    case 'text':
      if (block.note && noteAside && !isUserMessage) {
        return <NoteBlock content={block.content} blockId={blockId} blockOrder={blockOrder} />
      }
      if (isUserMessage) {
        return <UserMessageText text={block.content} blockId={blockId} blockOrder={blockOrder} />
      }
      return (
        <MarkdownContent className="text-sm" searchMatchIdPrefix={searchQuery ? blockId : undefined} searchOrder={searchQuery ? blockOrder : undefined}>
          {block.content}
        </MarkdownContent>
      )

    case 'thinking':
      return <ThinkingBlock content={block.content} collapsed={block.collapsed} done={block.done} tokens={block.tokens} blockId={blockId} blockOrder={blockOrder} />

    case 'tool':
      // Artifact/app authoring is slow BEFORE the tool executes (the model
      // streams the whole html as tool arguments) — a bare "running" pill
      // reads as a hang. Show a build card during that window; the normal
      // pill returns on completion (history rows are always status=done).
      if (
        block.status === 'running' &&
        (block.name === 'mcp__display__display_ui' || block.name === 'mcp__display__pin_app')
      ) {
        return (
          <div data-testid="ui-artifact-building" className={`my-2 w-full max-w-md overflow-hidden ${CARD}`}>
            <div className="flex items-center gap-3 px-4 py-3">
              <svg className="h-5 w-5 shrink-0 animate-spin text-tertiary" fill="none" viewBox="0 0 24 24">
                <circle className="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" strokeWidth="4" />
                <path className="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4z" />
              </svg>
              <span className="text-xs text-secondary">
                {block.name === 'mcp__display__pin_app'
                  ? 'Working on an app…'
                  : 'Working on an interactive artifact…'}
              </span>
            </div>
            <div className="animate-pulse space-y-2 px-4 pb-4">
              <div className="h-2.5 w-3/4 rounded-sm bg-surface-muted" />
              <div className="h-2.5 w-1/2 rounded-sm bg-surface-muted" />
              <div className="h-16 rounded-card bg-surface-muted" />
            </div>
          </div>
        )
      }
      return (
        <ToolActivity
          name={block.name}
          summary={block.summary}
          status={block.status}
          toolInput={block.toolInput}
          toolResult={block.toolResult}
          resultSummary={block.resultSummary}
        />
      )

    case 'subagent':
      return <SubagentInfo description={block.description} subagentType={block.subagentType} isActive={block.isActive} failed={block.failed} background={block._background} toolInput={block.toolInput} toolResult={block.toolResult} />

    case BG_COMMAND_BLOCK:
      return <BgCommandInfo command={block.command} description={block.description} isActive={block.isActive} failed={block.failed} toolInput={bgPair?.toolInput} toolResult={bgPair?.toolResult} />

    case 'delegate':
      return (
        <DelegateTaskInfo
          taskName={block.taskName}
          agent={block.agent}
          promptPreview={block.promptPreview}
          status={block.status}
          prompt={block.prompt}
          workerChatId={block.workerChatId}
        />
      )

    case 'delegate_files':
      return <DelegateResultFiles files={block.files} skipped={block.skipped} />

    case 'checkverdict':
      return (
        <CheckVerdictCard
          check={block.check} status={block.status} score={block.score} summary={block.summary}
          findings={block.findings} findingsTotal={block.findingsTotal} round={block.round}
          rounds={block.rounds} ranOn={block.ranOn} costUsd={block.costUsd}
        />
      )

    case 'permission':
      return (
        <PermissionDialog
          requestId={block.requestId}
          toolName={block.toolName}
          toolInput={block.toolInput}
          description={block.description}
          resolved={block.resolved}
          approved={block.approved}
          meetingAgent={block.meetingAgent}
          onRespond={onPermissionRespond}
        />
      )

    case 'question':
      return (
        <QuestionDialog
          toolInput={block.toolInput}
          answered={Boolean(block.answered || (dialogsInTerminal && block.followedInTurn))}
          onAnswer={onQuestionAnswer || (() => {})}
          requestId={block.requestId}
          onAnswerStructured={onQuestionAnswerStructured}
          readOnly={dialogsInTerminal}
        />
      )

    case 'setting':
      return (
        <SettingCard
          settingId={block.settingId}
          label={block.label}
          catalogVersion={block.catalogVersion}
        />
      )

    case 'card':
      return (
        <ActionCard
          cardId={block.cardId}
          title={block.title}
          text={block.text}
          note={block.note}
          buttons={block.buttons}
          options={block.options}
        />
      )

    case 'list':
      return (
        <ListCard
          kind={block.kind}
          filters={block.filters}
          title={block.title}
          agentName={agentName}
        />
      )

    case 'plan':
      return (
        <PlanView
          action={block.action}
          toolInput={block.toolInput}
          superseded={block.superseded}
          onImplement={dialogsInTerminal ? undefined : onImplementPlan}
          onImplementCodex={dialogsInTerminal ? undefined : onImplementPlanCodex}
          readOnly={dialogsInTerminal && !(block.resolved || block.followedInTurn)}
          onSendMessage={onSendMessage}
          onPlanFetched={onPlanFetched}
        />
      )

    case 'plan_review':
      return (
        <PlanReviewCard
          requestId={block.requestId}
          plan={block.plan}
          toolInput={block.toolInput}
          filename={block.filename}
          resolved={block.resolved}
          action={block.action}
          onRespond={onPlanReviewResponse}
          onSendMessage={onSendMessage}
          onPlanFetched={onPlanFetched}
        />
      )

    case 'system':
      return <SystemEvent subtype={block.subtype} agentName={block.agentName} agentColor={block.agentColor} message={block.message} reason={block.reason} returned={block.returned} sendAgain={sendAgainKind} onSendAgain={onSendAgain} />

    case 'images':
      return <ImageGallery images={block.images} />

    case 'video':
      return (
        <VideoPlayer
          src={block.srcKind === 'token' ? (block.mediaUrl || '') : (block.url || '')}
          mime={block.mime}
          poster={block.poster}
          caption={block.caption}
          title={block.title}
          downloadName={block.title || block.caption}
        />
      )

    case 'audio':
      return (
        <AudioPlayer
          src={block.srcKind === 'token' ? (block.mediaUrl || '') : (block.url || '')}
          mime={block.mime}
          caption={block.caption}
          title={block.title}
          downloadName={block.title || block.caption}
        />
      )

    case 'media_processing':
      return (
        <div className={`my-2 flex max-w-md items-center gap-3 px-4 py-3 ${CARD}`}>
          <svg className="h-5 w-5 animate-spin text-tertiary" fill="none" viewBox="0 0 24 24">
            <circle className="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" strokeWidth="4" />
            <path className="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4z" />
          </svg>
          <span className="text-xs text-secondary">
            Preparing {block.mediaKind === 'audio' ? 'audio' : 'video'} for playback…
          </span>
        </div>
      )

    case 'image_generating':
      return (
        <div className={`my-2 overflow-hidden max-w-md animate-pulse ${CARD}`}>
          <div className="w-full aspect-square bg-surface-muted flex items-center justify-center">
            <div className="text-center space-y-2">
              <svg className="w-8 h-8 mx-auto text-tertiary animate-spin" fill="none" viewBox="0 0 24 24">
                <circle className="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" strokeWidth="4" />
                <path className="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4z" />
              </svg>
              <p className="text-xs text-tertiary">Generating image...</p>
            </div>
          </div>
          <div className="px-3 py-2">
            <p className="text-xs text-tertiary truncate">
              {block.model === 'gpt-image' ? 'GPT Image 1.5' : 'Nano Banana Pro'}
              {block.promptPreview && `: ${block.promptPreview}`}
            </p>
          </div>
        </div>
      )

    case 'image_attachments': {
      // Per photo: a data URL on the live send, else a saved path drawn
      // through the agent files API; a bubble may mix the two (a photo handed
      // back from a cancelled queued message, then a new one).
      const srcs = block.images.map((src, i) => {
        if (src.startsWith('data:')) return src
        const path = block.paths?.[i]
        return path && agentName ? `/v1/agents/${agentName}/files/${encodeURI(path)}` : null
      })
      if (srcs.some(Boolean)) return <AttachedImageStrip srcs={srcs} />
      // Rows from before paths were stored, and views without an agent: the count.
      return (
        <div className="flex items-center gap-1.5 mb-1 text-xs text-action-fg">
          <svg className="w-3.5 h-3.5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
            <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d={PHOTO_ICON_PATH} />
          </svg>
          {block.images.length} image{block.images.length > 1 ? 's' : ''} attached
        </div>
      )
    }

    case 'file_attachments':
      return (
        <div className="flex flex-col gap-1 mb-1">
          {block.files.map((f, i) => (
            <div key={i} className="flex items-center gap-1.5 text-xs text-action-fg bg-action-fg/10 rounded-control px-2.5 py-1.5 w-fit">
              <svg className="w-3.5 h-3.5 shrink-0" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2}
                      d={f.folder ? FOLDER_ICON_PATH : FILE_ICON_PATH} />
              </svg>
              <span className="truncate max-w-[200px]">{f.name}</span>
            </div>
          ))}
        </div>
      )

    case 'url':
      return <DisplayUrl url={block.url} title={block.title} description={block.description} />

    case 'ui':
      if (uiSuperseded && block.path) {
        // The same artifact file is re-shown further down — collapse this
        // older copy to a chip (no iframe: it would only mirror the latest
        // content anyway, and every extra instance pays boot + live-reload).
        const scrollToLatest = () => {
          try {
            document
              .querySelector(`[data-ui-path="${CSS.escape(block.path!)}"]`)
              ?.scrollIntoView({ behavior: 'smooth', block: 'center' })
          } catch { /* CSS.escape unavailable — chip stays a plain marker */ }
        }
        return (
          <button
            onClick={scrollToLatest}
            data-testid="ui-artifact-superseded"
            className="my-1 flex items-center gap-1.5 rounded-control border border-subtle/60 bg-surface-muted/60 px-2.5 py-1.5 text-xs text-secondary transition-colors hover:bg-surface-muted"
          >
            <svg className="h-3.5 w-3.5 shrink-0 text-tertiary" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={1.5}>
              <path strokeLinecap="round" strokeLinejoin="round" d="M9 17.25v1.007a3 3 0 01-.879 2.122L7.5 21h9l-.621-.621A3 3 0 0115 18.257V17.25m6-12V15a2.25 2.25 0 01-2.25 2.25H5.25A2.25 2.25 0 013 15V5.25m18 0A2.25 2.25 0 0018.75 3H5.25A2.25 2.25 0 003 5.25m18 0V12a2.25 2.25 0 01-2.25 2.25H5.25A2.25 2.25 0 013 12V5.25" />
            </svg>
            <span className="font-medium">{block.title || uiTitle || 'UI artifact'}</span>
            <span className="text-tertiary">· updated version below ↓</span>
          </button>
        )
      }
      return (
        <UiArtifact
          token={block.token}
          uiUrl={block.uiUrl}
          title={block.title || uiTitle}
          height={block.height}
          path={block.path}
          agent={agentName}
          onInteraction={onArtifactInteraction}
        />
      )

    case 'artifact_interaction': {
      // Provenance chip: a page event delivered from an artifact — visibly
      // NOT a user message.
      const payloadPreview = (() => {
        try {
          const s = JSON.stringify(block.payload)
          return s && s.length > 120 ? s.slice(0, 117) + '…' : s || ''
        } catch { return '' }
      })()
      return (
        <div className="my-1 flex items-start gap-1.5 rounded-card border border-subtle/60 bg-surface-muted/60 px-2.5 py-1.5 text-xs">
          <svg className="mt-0.5 h-3.5 w-3.5 shrink-0 text-tertiary" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={1.5}>
            <path strokeLinecap="round" strokeLinejoin="round" d="M3.75 13.5l10.5-11.25L12 10.5h8.25L9.75 21.75 12 13.5H3.75z" />
          </svg>
          <div className="min-w-0">
            <span className="font-medium text-secondary">
              Interaction from artifact{block.title ? ` “${block.title}”` : ''}
            </span>
            {payloadPreview && (
              <code className="ml-1.5 break-all text-2xs text-tertiary">{payloadPreview}</code>
            )}
          </div>
        </div>
      )
    }

    case 'app_action': {
      // Provenance chip: a declared app action delivered into the chat —
      // visibly NOT a user message.
      const promptPreview = (block.prompt || '').length > 120
        ? (block.prompt || '').slice(0, 117) + '…'
        : block.prompt || ''
      return (
        <div className="my-1 flex items-start gap-1.5 rounded-card border border-subtle/60 bg-surface-muted/60 px-2.5 py-1.5 text-xs">
          <svg className="mt-0.5 h-3.5 w-3.5 shrink-0 text-tertiary" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={1.5}>
            <rect x="3.75" y="3.75" width="7" height="7" rx="1.5" />
            <rect x="13.25" y="3.75" width="7" height="7" rx="1.5" />
            <rect x="3.75" y="13.25" width="7" height="7" rx="1.5" />
            <rect x="13.25" y="13.25" width="7" height="7" rx="1.5" />
          </svg>
          <div className="min-w-0">
            <span className="font-medium text-secondary">
              Action from app{block.title ? ` “${block.title}”` : ''}{block.label ? `: ${block.label}` : ''}
            </span>
            {promptPreview && (
              <span className="ml-1.5 break-all text-2xs text-tertiary">{promptPreview}</span>
            )}
          </div>
        </div>
      )
    }

    case 'file':
      return (
        <DisplayFile
          filename={block.filename}
          downloadUrl={block.downloadUrl}
          description={block.description}
        />
      )

    case 'document_preview':
      // A card: the document opens in the chat's document pane.
      return (
        <DocumentCard
          chatId={chatId}
          fileId={block.fileId}
          filename={block.filename}
          downloadUrl={block.downloadUrl}
          snapshotId={block.snapshotId}
          dbMessageId={block.dbMessageId}
          version={block.version}
          generation={block.generation}
        />
      )

    case 'metadata':
      // Rendered in the message footer row (copy + badges), not as a block
      return null

    default:
      return null
  }
}
