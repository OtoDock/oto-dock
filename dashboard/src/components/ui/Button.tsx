import type { ComponentPropsWithRef } from 'react'

import { BUTTON_BASE, BUTTON_SIZE, BUTTON_VARIANT } from './classes'

export type ButtonVariant = keyof typeof BUTTON_VARIANT
export type ButtonSize = keyof typeof BUTTON_SIZE

type Props = ComponentPropsWithRef<'button'> & {
  variant?: ButtonVariant
  size?: ButtonSize
}

/**
 * The dashboard's button: a native
 * <button>, `type="button"` unless told otherwise (a native button inside a
 * form submits it), a visible focus ring, and `className` for its place in
 * the layout (`w-full`, `flex-1`, `ml-auto`).
 */
export default function Button({ variant = 'secondary', size = 'sm', type = 'button', className = '', ...rest }: Props) {
  return <button type={type} className={`${BUTTON_BASE} ${BUTTON_VARIANT[variant]} ${BUTTON_SIZE[size]} ${className}`} {...rest} />
}
