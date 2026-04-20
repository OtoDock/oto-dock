import { getFileIconStyle } from '../../lib/fileTypes'

/** Letter/glyph badge for a file. Matches the VS-Code-inspired tree style. */
export default function FileIcon({ name, size = 16 }: { name: string; size?: number }) {
  const style = getFileIconStyle(name)
  return (
    <span
      className={`flex items-center justify-center font-bold leading-none ${style.color} shrink-0`}
      style={{ width: size, height: size, fontSize: size <= 16 ? '9px' : Math.round(size * 0.45) }}
    >
      {style.icon}
    </span>
  )
}

/** Folder icon, open/closed variants. */
export function FolderIcon({ open, size = 16 }: { open: boolean; size?: number }) {
  const baseClass = 'text-amber-500 shrink-0'
  if (open) {
    return (
      <svg
        className={baseClass}
        width={size}
        height={size}
        fill="currentColor"
        viewBox="0 0 20 20"
      >
        <path d="M2 6a2 2 0 012-2h5l2 2h5a2 2 0 012 2v1H2V6z" />
        <path fillRule="evenodd" d="M2 9.5h16l-1.6 6.4A2 2 0 0114.46 17.5H5.54a2 2 0 01-1.94-1.6L2 9.5z" clipRule="evenodd" />
      </svg>
    )
  }
  return (
    <svg
      className={baseClass}
      width={size}
      height={size}
      fill="currentColor"
      viewBox="0 0 20 20"
    >
      <path d="M2 6a2 2 0 012-2h5l2 2h5a2 2 0 012 2v6a2 2 0 01-2 2H4a2 2 0 01-2-2V6z" />
    </svg>
  )
}
