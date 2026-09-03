import type { ResolvedChatPath } from '../../api/chats'
import { getFileKind } from '../../lib/fileTypes'
import { encodePathSegments } from '../../lib/paths'
import FilePreviewBody from '../workspace/FilePreviewBody'
import FilePreviewPortal from '../workspace/FilePreviewPortal'

interface Props {
  resolved: ResolvedChatPath
  onClose: () => void
}

/**
 * Full-screen preview for a resolved file-path chip. `FilePreviewBody` IS the
 * modal — every branch renders its own `FilePreviewPortal` chrome (header,
 * download, close X, Esc, scroll-lock) — so this is just the node mapping
 * plus one routing guard, no wrapper.
 *
 * `canWrite` is always false: it drives the WOPI `edit:` flag and editor
 * writability, and the resolve endpoint is read-only by construction.
 *
 * Routing guard: `previewable: false` on a Collabora-document extension
 * (e.g. `knowledge/report.docx` — outside the wopi-url confinement) must NOT
 * reach FilePreviewBody's document branch, which would fire the doomed
 * `/v1/documents/wopi-url` call and render the raw error. Those files get the
 * download-only portal directly. Media/text/images are unaffected (their
 * fetches are path-based and role-gated server-side).
 */
export default function ChatFilePreview({ resolved, onClose }: Props) {
  const { agent, path, filename, size, previewable } = resolved

  if (!previewable && getFileKind(filename) === 'document') {
    // Same download URL shape as FilePreviewBody (`fn=` feeds the Android
    // DownloadListener; browsers use the `download` attribute).
    const downloadUrl = `/v1/agents/${encodeURIComponent(agent)}/files/${encodePathSegments(path)}?download=true&fn=${encodeURIComponent(filename)}`
    return (
      <FilePreviewPortal filename={filename} onClose={onClose} downloadUrl={downloadUrl}>
        <div className="h-full flex items-center justify-center text-white/80 text-sm">
          Preview unavailable for this file. Use the download button above.
        </div>
      </FilePreviewPortal>
    )
  }

  return (
    <FilePreviewBody
      agent={agent}
      // `modified` is required by the FileNode type; only name/path are read.
      node={{ path, name: filename, type: 'file', size, modified: '' }}
      canWrite={false}
      onClose={onClose}
    />
  )
}
