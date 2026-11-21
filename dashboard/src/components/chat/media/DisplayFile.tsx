import { useRef, useState, type MouseEvent } from 'react'
import { safeHref } from '../../../lib/safeUrl'
import { fileDragRef, writeFileDrag } from '../../../lib/chat/fileDrag'
import { checkedDownload, withName } from './documentDownloads'

interface Props {
  filename: string
  downloadUrl: string
  description?: string
}

export default function DisplayFile({ filename, downloadUrl, description }: Props) {
  // The filename rides as a query param (`fn`): the Android app's download
  // listener names the saved file from it.
  const url = withName(downloadUrl, filename)
  // A media download drags to another chat's message box (lib/chat/fileDrag).
  const ref = fileDragRef(downloadUrl, filename)
  const href = safeHref(url)
  // A media download asks its link first and says on the card, under its
  // name, why it did not start, until a later tap starts it. Any other link
  // (a share's copy) and a click with a key held stay the browser's own.
  const [failure, setFailure] = useState('')
  const asking = useRef(false)
  const onClick = (e: MouseEvent<HTMLAnchorElement>) => {
    if (!href || !ref || e.button !== 0 || e.metaKey || e.ctrlKey || e.shiftKey || e.altKey) return
    e.preventDefault()
    if (asking.current) return
    asking.current = true
    void checkedDownload(href, filename).then(setFailure).finally(() => { asking.current = false })
  }
  return (
    <a
      href={href}
      download={filename}
      onClick={onClick}
      data-file-card=""
      draggable={ref ? true : undefined}
      onDragStart={ref ? (e) => writeFileDrag(e.dataTransfer, ref, url) : undefined}
      title={ref ? 'Drag it to another chat to attach it there.' : undefined}
      className="block my-2 p-3 rounded-card border border-subtle bg-surface-muted/50 hover:bg-surface-muted transition-colors"
    >
      <div className="flex items-center gap-2">
        <span className="text-secondary">&#128196;</span>
        <span className="text-sm font-medium text-primary">{filename}</span>
        <span className="text-xs text-tertiary">&#8595; Download</span>
      </div>
      {description && <p className="text-xs text-secondary mt-1">{description}</p>}
      {failure && <p role="alert" className="mt-1 text-xs text-status-warning-fg">{failure}</p>}
    </a>
  )
}
