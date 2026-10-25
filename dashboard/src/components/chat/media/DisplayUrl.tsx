import { safeHref } from '../../../lib/safeUrl'

interface Props {
  url: string
  title: string
  description?: string
}

export default function DisplayUrl({ url, title, description }: Props) {
  return (
    <a
      href={safeHref(url)}
      target="_blank"
      rel="noopener noreferrer"
      className="block my-2 p-3 rounded-card bg-surface-card shadow-elevation-1 hover:bg-surface-hover transition-colors"
    >
      <div className="flex items-center gap-2">
        <span className="text-link">&#128279;</span>
        <span className="text-sm font-medium text-link">{title || url}</span>
        <span className="text-xs text-link">&#8599;</span>
      </div>
      {description && <p className="text-xs text-secondary mt-1">{description}</p>}
    </a>
  )
}
