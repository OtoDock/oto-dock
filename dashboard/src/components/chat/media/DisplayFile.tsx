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
      className="block my-2 p-3 rounded-xl border border-p-border-light bg-p-surface/50 hover:bg-p-surface transition-colors"
    >
      <div className="flex items-center gap-2">
        <span className="text-p-text-secondary">&#128196;</span>
        <span className="text-sm font-medium text-p-text">{filename}</span>
        <span className="text-xs text-p-text-light">&#8595; Download</span>
      </div>
      {description && <p className="text-xs text-p-text-secondary mt-1">{description}</p>}
    </a>
  )
}
