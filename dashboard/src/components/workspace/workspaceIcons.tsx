export type IconName =
  | 'open' | 'pencil' | 'download' | 'link' | 'trash'
  | 'scissors' | 'copy' | 'clipboard' | 'folder' | 'upload'

export function Icon({ name }: { name: IconName }) {
  const common = 'w-3.5 h-3.5'
  switch (name) {
    case 'open':
      return (
        <svg className={common} fill="none" stroke="currentColor" viewBox="0 0 24 24" strokeWidth={2}>
          <path strokeLinecap="round" strokeLinejoin="round" d="M15 12h6m0 0l-3-3m3 3l-3 3M3 6a2 2 0 012-2h4l2 2h4a2 2 0 012 2v8" />
        </svg>
      )
    case 'pencil':
      return (
        <svg className={common} fill="none" stroke="currentColor" viewBox="0 0 24 24" strokeWidth={2}>
          <path strokeLinecap="round" strokeLinejoin="round" d="M11 5H6a2 2 0 00-2 2v11a2 2 0 002 2h11a2 2 0 002-2v-5m-1.414-9.414a2 2 0 112.828 2.828L11.828 15H9v-2.828l8.586-8.586z" />
        </svg>
      )
    case 'scissors':
      return (
        <svg className={common} fill="none" stroke="currentColor" viewBox="0 0 24 24" strokeWidth={2}>
          <circle cx="6" cy="6" r="3" />
          <circle cx="6" cy="18" r="3" />
          <line x1="20" y1="4" x2="8.12" y2="15.88" />
          <line x1="14.47" y1="14.48" x2="20" y2="20" />
          <line x1="8.12" y1="8.12" x2="12" y2="12" />
        </svg>
      )
    case 'copy':
      return (
        <svg className={common} fill="none" stroke="currentColor" viewBox="0 0 24 24" strokeWidth={2}>
          <rect x="9" y="9" width="13" height="13" rx="2" />
          <path strokeLinecap="round" strokeLinejoin="round" d="M5 15H4a2 2 0 01-2-2V4a2 2 0 012-2h9a2 2 0 012 2v1" />
        </svg>
      )
    case 'clipboard':
      return (
        <svg className={common} fill="none" stroke="currentColor" viewBox="0 0 24 24" strokeWidth={2}>
          <rect x="8" y="4" width="8" height="3" rx="1" />
          <path strokeLinecap="round" strokeLinejoin="round" d="M5 7h2v13a1 1 0 001 1h8a1 1 0 001-1V7h2" />
        </svg>
      )
    case 'folder':
      return (
        <svg className={common} fill="currentColor" viewBox="0 0 24 24">
          <path d="M3 7a2 2 0 012-2h4l2 2h7a2 2 0 012 2v8a2 2 0 01-2 2H5a2 2 0 01-2-2V7z" />
        </svg>
      )
    case 'upload':
      return (
        <svg className={common} fill="none" stroke="currentColor" viewBox="0 0 24 24" strokeWidth={2}>
          <path strokeLinecap="round" strokeLinejoin="round" d="M4 16v1a3 3 0 003 3h10a3 3 0 003-3v-1m-4-4l-4-4m0 0l-4 4m4-4v12" />
        </svg>
      )
    case 'download':
      return (
        <svg className={common} fill="none" stroke="currentColor" viewBox="0 0 24 24" strokeWidth={2}>
          <path strokeLinecap="round" strokeLinejoin="round" d="M4 16v1a3 3 0 003 3h10a3 3 0 003-3v-1m-4-4l-4 4m0 0l-4-4m4 4V4" />
        </svg>
      )
    case 'link':
      return (
        <svg className={common} fill="none" stroke="currentColor" viewBox="0 0 24 24" strokeWidth={2}>
          <path strokeLinecap="round" strokeLinejoin="round" d="M13.828 10.172a4 4 0 015.656 0l1.415 1.414a4 4 0 010 5.657l-2.122 2.121a4 4 0 01-5.656 0M10.172 13.828a4 4 0 01-5.656 0L3.1 12.414a4 4 0 010-5.657L5.222 4.636a4 4 0 015.656 0" />
        </svg>
      )
    case 'trash':
      return (
        <svg className={common} fill="currentColor" viewBox="0 0 16 16">
          <path fillRule="evenodd" d="M5 3.25V4H2.75a.75.75 0 000 1.5h.3l.815 8.15A1.5 1.5 0 005.357 15h5.285a1.5 1.5 0 001.493-1.35l.815-8.15h.3a.75.75 0 000-1.5H11v-.75A2.25 2.25 0 008.75 1h-1.5A2.25 2.25 0 005 3.25zm2.25-.75a.75.75 0 00-.75.75V4h3v-.75a.75.75 0 00-.75-.75h-1.5z" clipRule="evenodd" />
        </svg>
      )
  }
}
