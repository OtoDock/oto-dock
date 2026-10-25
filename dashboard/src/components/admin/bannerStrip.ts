// The strip an admin banner draws across the top of a page: one shape, its
// tone by meaning (warning when it asks for an action, info when it informs).
export const BANNER_STRIP = 'border-b px-4 py-2.5 flex items-start justify-between gap-3 relative z-50'

export const BANNER_TONE = {
  warning: 'bg-status-warning-bg border-status-warning-border',
  info: 'bg-status-info-bg border-status-info-border',
} as const
