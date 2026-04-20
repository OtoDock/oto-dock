import type { MessageBlock } from '../types'
import ImageGallery from '../media/ImageGallery'
import VideoPlayer from '../media/VideoPlayer'
import AudioPlayer from '../media/AudioPlayer'
import DisplayUrl from '../media/DisplayUrl'
import DisplayFile from '../media/DisplayFile'
import DocumentPreview from '../media/DocumentPreview'
import UiArtifact from '../media/UiArtifact'

/**
 * Renders a single display/file-tools artifact block (gallery/chart, video,
 * audio, url, file, Collabora preview) using the same renderer components as
 * ChatMessages' BlockRenderer — so a prop change to any renderer breaks both
 * call sites at compile time (no drift). Used by the interactive-CLI PiP
 * floating windows, which have no inline message list.
 *
 * Returns null for any non-artifact block. `embedded` is forwarded to
 * DocumentPreview so a PiP window supplies the chrome (no duplicate header/close).
 */
export default function ArtifactView({ block, agent, embedded, onArtifactInteraction }: { block: MessageBlock; agent?: string; embedded?: boolean; onArtifactInteraction?: (token: string, title: string, payload: unknown) => Promise<{ status: string; reason?: string }> }) {
  switch (block.type) {
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
        <div className="flex max-w-md items-center gap-3 rounded-xl border border-p-border-light bg-white px-4 py-3 dark:bg-p-surface">
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
        <div className="rounded-xl border border-p-border-light bg-white dark:bg-p-surface overflow-hidden max-w-md animate-pulse">
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
    case 'url':
      return <DisplayUrl url={block.url} title={block.title} description={block.description} />
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
          embedded={embedded}
        />
      )
    case 'ui':
      return (
        <UiArtifact
          token={block.token}
          uiUrl={block.uiUrl}
          title={block.title}
          height={block.height}
          path={block.path}
          agent={agent}
          embedded={embedded}
          onInteraction={onArtifactInteraction}
        />
      )
    default:
      return null
  }
}
