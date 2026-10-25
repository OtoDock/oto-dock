import { safeHref } from '../../../lib/safeUrl'

interface Props {
  filename: string
  downloadUrl: string
  description?: string
}

export default function DisplayFile({ filename, downloadUrl, description }: Props) {
  // Append filename as query param so Android WebView DownloadListener can
  // extract it (the listener fires before HTTP response headers arrive,
  // so Content-Disposition is unavailable and URLUtil.guessFileName falls back to .bin)
  const url = `${downloadUrl}${downloadUrl.includes('?') ? '&' : '?'}fn=${encodeURIComponent(filename)}`
  return (
    <a
      href={safeHref(url)}
      download={filename}
      className="block my-2 p-3 rounded-card border border-subtle bg-surface-muted/50 hover:bg-surface-muted transition-colors"
    >
      <div className="flex items-center gap-2">
        <span className="text-secondary">&#128196;</span>
        <span className="text-sm font-medium text-primary">{filename}</span>
        <span className="text-xs text-tertiary">&#8595; Download</span>
      </div>
      {description && <p className="text-xs text-secondary mt-1">{description}</p>}
    </a>
  )
}
