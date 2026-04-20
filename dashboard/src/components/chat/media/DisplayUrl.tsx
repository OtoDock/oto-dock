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
      className="block my-2 p-3 rounded-xl border border-brand/20 bg-brand-50 hover:bg-brand-100 transition-colors"
    >
      <div className="flex items-center gap-2">
        <span className="text-brand">&#128279;</span>
        <span className="text-sm font-medium text-brand">{title || url}</span>
        <span className="text-xs text-brand-light">&#8599;</span>
      </div>
      {description && <p className="text-xs text-brand/80 mt-1">{description}</p>}
    </a>
  )
}
