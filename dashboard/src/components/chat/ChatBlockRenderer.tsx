import { type JSX, useState } from 'react'
import MarkdownContent from './MarkdownContent'
import ImageLightbox, { type LightboxImage } from './media/ImageLightbox'
import ToolActivity from './ToolActivity'
import ThinkingBlock from './ThinkingBlock'
import PermissionDialog from './PermissionDialog'
import SubagentInfo from './SubagentInfo'
import BgCommandInfo from './BgCommandInfo'
import DelegateTaskInfo from './DelegateTaskInfo'
import SystemEvent from './SystemEvent'
import ImageGallery from './media/ImageGallery'
import VideoPlayer from './media/VideoPlayer'
import AudioPlayer from './media/AudioPlayer'
import DisplayUrl from './media/DisplayUrl'
import DisplayFile from './media/DisplayFile'
import DocumentPreview from './media/DocumentPreview'
import UiArtifact from './media/UiArtifact'
import PlanView from './plan/PlanView'
import QuestionDialog from './QuestionDialog'
import SearchHighlight from './SearchHighlight'
import { useSearch } from '../../contexts/SearchContext'
import type { MessageBlock } from './types'
import type { BgCommandPair, PreviewChainMode } from '../../lib/messageBlocks'
import PlanReviewCard from './PlanReviewCard'

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

// Live chat-attached photos (base64 data URLs). Opens the shared lightbox on
// click — Chromium blocks window.open on data: URLs, so a new-tab navigation
// would silently do nothing.
function AttachedImageStrip({ images }: { images: string[] }) {
  const [lightboxIdx, setLightboxIdx] = useState<number | null>(null)
  return (
    <div className="flex gap-2 flex-wrap mb-1">
      {images.map((src, i) => (
        <img
          key={i}
          src={src}
          alt={`Attached image ${i + 1}`}
          className="w-20 h-20 rounded-lg object-cover border border-white/30 cursor-pointer
                     hover:opacity-90 transition-opacity"
          onClick={() => setLightboxIdx(i)}
        />
      ))}
      {lightboxIdx !== null && (
        <ImageLightbox
          images={images.map((src): LightboxImage => {
            const m = src.match(/^data:([^;,]+);base64,(.*)$/)
            return m ? { imageData: m[2], mimeType: m[1] } : { url: src }
          })}
          initialIndex={lightboxIdx}
          onClose={() => setLightboxIdx(null)}
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
  onPlanFetched,
  onDismissPreview,
  onArtifactInteraction,
  bgPair,
  agentName,
  uiSuperseded,
  uiTitle,
  previewMode,
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
  onPlanFetched?: (filename: string, content: string) => void
  /** document_preview blocks: `key` scopes the removal to ONE instance (a
   * frozen "previous version" closing itself); undefined removes the file's
   * whole preview trail (the live block's close). */
  onDismissPreview?: (fileId: string, key?: { snapshotId?: string; dbMessageId?: number }) => void
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
  /** document_preview blocks: render-time chain state (previewChainModes) —
   * live / frozen previous version / chip. */
  previewMode?: PreviewChainMode
}) {
  const { query: searchQuery } = useSearch()
  switch (block.type) {
    case 'text':
      if (isUserMessage) {
        return (
          <p className="text-sm whitespace-pre-wrap">
            {linkifyText(block.content, searchQuery ? blockId : undefined, searchQuery ? blockOrder : undefined)}
          </p>
        )
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
          <div data-testid="ui-artifact-building" className="my-2 w-full max-w-md overflow-hidden rounded-xl border border-p-border-light bg-white dark:bg-p-surface">
            <div className="flex items-center gap-3 px-4 py-3">
              <svg className="h-5 w-5 shrink-0 animate-spin text-p-text-light" fill="none" viewBox="0 0 24 24">
                <circle className="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" strokeWidth="4" />
                <path className="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4z" />
              </svg>
              <span className="text-xs text-p-text-secondary">
                {block.name === 'mcp__display__pin_app'
                  ? 'Working on a mini-app…'
                  : 'Working on an interactive artifact…'}
              </span>
            </div>
            <div className="animate-pulse space-y-2 px-4 pb-4">
              <div className="h-2.5 w-3/4 rounded bg-gray-200 dark:bg-gray-700" />
              <div className="h-2.5 w-1/2 rounded bg-gray-200 dark:bg-gray-700" />
              <div className="h-16 rounded-lg bg-linear-to-br from-gray-100 to-gray-200 dark:from-gray-800 dark:to-gray-700" />
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
      return <SubagentInfo description={block.description} subagentType={block.subagentType} isActive={block.isActive} failed={block.failed} background={(block as any)._background} toolInput={block.toolInput} toolResult={block.toolResult} />

    case 'bgcommand':
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

    case 'schedulewake':
      // A schedule_continuation wake drove this turn (no user present).
      return (
        <div className="my-1.5 flex items-center gap-2 py-1.5 px-2 rounded-lg bg-p-surface text-xs text-p-text-secondary">
          <span className="shrink-0" aria-hidden>⏰</span>
          <span className="shrink-0 font-medium">Scheduled wake</span>
          {block.prompt && <span className="truncate text-p-text-light">{block.prompt}</span>}
        </div>
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
          answered={block.answered}
          onAnswer={onQuestionAnswer || (() => {})}
          requestId={block.requestId}
          onAnswerStructured={onQuestionAnswerStructured}
        />
      )

    case 'plan':
      return (
        <PlanView
          action={block.action}
          toolInput={block.toolInput}
          superseded={block.superseded}
          onImplement={onImplementPlan}
          onImplementCodex={onImplementPlanCodex}
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
      return <SystemEvent subtype={block.subtype} agentName={block.agentName} agentColor={block.agentColor} message={block.message} />

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
        <div className="my-2 flex max-w-md items-center gap-3 rounded-xl border border-p-border-light bg-white px-4 py-3 dark:bg-p-surface">
          <svg className="h-5 w-5 animate-spin text-p-text-light" fill="none" viewBox="0 0 24 24">
            <circle className="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" strokeWidth="4" />
            <path className="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4z" />
          </svg>
          <span className="text-xs text-p-text-secondary">
            Preparing {block.mediaKind === 'audio' ? 'audio' : 'video'} for playback…
          </span>
        </div>
      )

    case 'image_generating':
      return (
        <div className="my-2 rounded-xl border border-p-border-light bg-white dark:bg-p-surface overflow-hidden max-w-md animate-pulse">
          <div className="w-full aspect-square bg-linear-to-br from-gray-100 to-gray-200 dark:from-gray-800 dark:to-gray-700 flex items-center justify-center">
            <div className="text-center space-y-2">
              <svg className="w-8 h-8 mx-auto text-gray-400 dark:text-gray-500 animate-spin" fill="none" viewBox="0 0 24 24">
                <circle className="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" strokeWidth="4" />
                <path className="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4z" />
              </svg>
              <p className="text-xs text-gray-500 dark:text-gray-400">Generating image...</p>
            </div>
          </div>
          <div className="px-3 py-2">
            <p className="text-xs text-p-text-light truncate">
              {block.model === 'gpt-image' ? 'GPT Image 1.5' : 'Nano Banana Pro'}
              {block.promptPreview && ` — ${block.promptPreview}`}
            </p>
          </div>
        </div>
      )

    case 'image_attachments': {
      // Images are base64 data URLs during live session, filenames on history reload
      const isLive = block.images.length > 0 && block.images[0].startsWith('data:')
      if (isLive) {
        return <AttachedImageStrip images={block.images} />
      }
      // History reload: the saved upload paths render through the agent files
      // API (same-origin session cookie). Only when EVERY image has a path —
      // pre-path rows (and task views without an agent) keep the count badge.
      const fileUrls = agentName
        ? (block.paths ?? []).flatMap((p) =>
            p ? [`/v1/agents/${agentName}/files/${encodeURI(p)}`] : [])
        : []
      if (fileUrls.length > 0 && fileUrls.length === block.images.length) {
        return <AttachedImageStrip images={fileUrls} />
      }
      // Legacy rows without saved paths: badge with count
      return (
        <div className="flex items-center gap-1.5 mb-1 text-xs text-white/70">
          <svg className="w-3.5 h-3.5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
            <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2}
                  d="M4 16l4.586-4.586a2 2 0 012.828 0L16 16m-2-2l1.586-1.586a2 2 0 012.828 0L20 14m-6-6h.01M6 20h12a2 2 0 002-2V6a2 2 0 00-2-2H6a2 2 0 00-2 2v12a2 2 0 002 2z" />
          </svg>
          {block.images.length} image{block.images.length > 1 ? 's' : ''} attached
        </div>
      )
    }

    case 'file_attachments':
      return (
        <div className="flex flex-col gap-1 mb-1">
          {block.files.map((f, i) => (
            <div key={i} className="flex items-center gap-1.5 text-xs text-white/80 bg-white/10 rounded-md px-2.5 py-1.5 w-fit">
              <svg className="w-3.5 h-3.5 shrink-0" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2}
                      d="M9 12h6m-6 4h6m2 5H7a2 2 0 01-2-2V5a2 2 0 012-2h5.586a1 1 0 01.707.293l5.414 5.414a1 1 0 01.293.707V19a2 2 0 01-2 2z" />
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
            className="my-1 flex items-center gap-1.5 rounded-lg border border-p-border-light/60 bg-p-surface/60 px-2.5 py-1.5 text-xs text-p-text-secondary transition-colors hover:bg-p-surface"
          >
            <svg className="h-3.5 w-3.5 shrink-0 text-p-text-light" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={1.5}>
              <path strokeLinecap="round" strokeLinejoin="round" d="M9 17.25v1.007a3 3 0 01-.879 2.122L7.5 21h9l-.621-.621A3 3 0 0115 18.257V17.25m6-12V15a2.25 2.25 0 01-2.25 2.25H5.25A2.25 2.25 0 013 15V5.25m18 0A2.25 2.25 0 0018.75 3H5.25A2.25 2.25 0 003 5.25m18 0V12a2.25 2.25 0 01-2.25 2.25H5.25A2.25 2.25 0 013 12V5.25" />
            </svg>
            <span className="font-medium">{block.title || uiTitle || 'UI artifact'}</span>
            <span className="text-p-text-light">— updated version below ↓</span>
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
        <div className="my-1 flex items-start gap-1.5 rounded-lg border border-p-border-light/60 bg-p-surface/60 px-2.5 py-1.5 text-xs">
          <svg className="mt-0.5 h-3.5 w-3.5 shrink-0 text-p-text-light" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={1.5}>
            <path strokeLinecap="round" strokeLinejoin="round" d="M3.75 13.5l10.5-11.25L12 10.5h8.25L9.75 21.75 12 13.5H3.75z" />
          </svg>
          <div className="min-w-0">
            <span className="font-medium text-p-text-secondary">
              interaction from artifact{block.title ? ` “${block.title}”` : ''}
            </span>
            {payloadPreview && (
              <code className="ml-1.5 break-all text-[11px] text-p-text-light">{payloadPreview}</code>
            )}
          </div>
        </div>
      )
    }

    case 'app_action': {
      // Provenance chip: a declared mini-app action delivered into the chat —
      // visibly NOT a user message.
      const promptPreview = (block.prompt || '').length > 120
        ? (block.prompt || '').slice(0, 117) + '…'
        : block.prompt || ''
      return (
        <div className="my-1 flex items-start gap-1.5 rounded-lg border border-p-border-light/60 bg-p-surface/60 px-2.5 py-1.5 text-xs">
          <svg className="mt-0.5 h-3.5 w-3.5 shrink-0 text-p-text-light" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={1.5}>
            <rect x="3.75" y="3.75" width="7" height="7" rx="1.5" />
            <rect x="13.25" y="3.75" width="7" height="7" rx="1.5" />
            <rect x="3.75" y="13.25" width="7" height="7" rx="1.5" />
            <rect x="13.25" y="13.25" width="7" height="7" rx="1.5" />
          </svg>
          <div className="min-w-0">
            <span className="font-medium text-p-text-secondary">
              action from mini-app{block.title ? ` “${block.title}”` : ''}{block.label ? ` — ${block.label}` : ''}
            </span>
            {promptPreview && (
              <span className="ml-1.5 break-all text-[11px] text-p-text-light">{promptPreview}</span>
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
      return (
        <DocumentPreview
          wopiUrl={block.wopiUrl}
          filename={block.filename}
          fileId={block.fileId}
          downloadUrl={block.downloadUrl}
          dbMessageId={block.dbMessageId}
          snapshotId={block.snapshotId}
          chatId={chatId}
          generation={block.generation}
          mode={previewMode}
          onDismiss={(scope) => onDismissPreview?.(
            block.fileId,
            scope === 'instance'
              ? { snapshotId: block.snapshotId, dbMessageId: block.dbMessageId }
              : undefined,
          )}
        />
      )

    case 'metadata':
      // Rendered in the message footer row (copy + badges), not as a block
      return null

    default:
      return null
  }
}
