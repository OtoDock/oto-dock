import type { ComponentPropsWithRef } from 'react'

import { FIELD, FIELD_BORDER, FIELD_COMPACT, FIELD_INVALID, FIELD_REGULAR, LABEL } from './classes'

function fieldClass(compact: boolean, invalid: boolean, extra: string): string {
  return `${FIELD} ${compact ? FIELD_COMPACT : FIELD_REGULAR} ${invalid ? FIELD_INVALID : FIELD_BORDER} ${extra}`
}

// `compact`, not `size`: the native elements already have a `size` attribute.
type InputProps = ComponentPropsWithRef<'input'> & { compact?: boolean; invalid?: boolean }

/**
 * A text field: the native <input> with
 * the one field look. `invalid` sets `aria-invalid` and the danger border. Its
 * text size is the same on every device ("Fields on a touch device").
 */
export function Input({ compact = false, invalid = false, className = '', ...rest }: InputProps) {
  return <input aria-invalid={invalid || undefined} className={fieldClass(compact, invalid, className)} {...rest} />
}

type SelectProps = ComponentPropsWithRef<'select'> & { compact?: boolean; invalid?: boolean }

/**
 * A choice field: the native <select> on purpose, so a phone opens its own
 * picker (the iOS wheel, the Android sheet) and the dark theme's
 * `color-scheme` paints the option list. A custom popup is for choices a
 * <select> cannot show (icons, the mode and model pickers).
 */
export function Select({ compact = false, invalid = false, className = '', ...rest }: SelectProps) {
  return <select aria-invalid={invalid || undefined} className={fieldClass(compact, invalid, className)} {...rest} />
}

type LabelProps = ComponentPropsWithRef<'label'>

/** A field's label: name the field with `htmlFor`. */
export function Label({ className = '', ...rest }: LabelProps) {
  return <label className={`${LABEL} ${className}`} {...rest} />
}
