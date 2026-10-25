import { SWITCH_OFF, SWITCH_ON, SWITCH_THUMB, SWITCH_TRACK } from './classes'

type Label = { 'aria-label': string; 'aria-labelledby'?: never } | { 'aria-labelledby': string; 'aria-label'?: never }

type Props = Label & {
  checked: boolean
  onChange: (next: boolean) => void
  disabled?: boolean
  id?: string
  /** What the current state means, on a hover. */
  title?: string
  className?: string
}

/**
 * An on/off switch: a button with the
 * switch role, named by `aria-label` or `aria-labelledby` (the type asks
 * for one), Space or Enter toggles it, and the focus shows.
 */
export default function Switch({ checked, onChange, disabled = false, id, title, className = '', ...label }: Props) {
  return (
    <button
      type="button"
      role="switch"
      id={id}
      title={title}
      aria-checked={checked}
      aria-label={label['aria-label']}
      aria-labelledby={label['aria-labelledby']}
      disabled={disabled}
      onClick={() => onChange(!checked)}
      className={`${SWITCH_TRACK} ${checked ? SWITCH_ON : SWITCH_OFF} ${className}`}
    >
      <span className={`${SWITCH_THUMB} ${checked ? 'translate-x-4' : 'translate-x-0'}`} />
    </button>
  )
}
