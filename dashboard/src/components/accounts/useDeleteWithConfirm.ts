import { useState } from 'react'
import {
  linkedTriggersOf,
  useDeleteSubscription,
  type WebhookSubscription,
} from '../../api/subscriptions'
import { useDialogs } from '../ui/dialogs'

/** A subscription OtoDock relays to this install (not registered at the vendor by it). */
export function isRelayDelivery(row: Pick<WebhookSubscription, 'delivery_mode'>): boolean {
  return row.delivery_mode === 'relay'
}

/**
 * Delete with the two confirmations: the plain one, then, when the server
 * answers that triggers still fire from the row, a second one before the
 * forced delete leaves those triggers without a source.
 */
export function useDeleteWithConfirm() {
  const dialogs = useDialogs()
  const del = useDeleteSubscription()
  const [error, setError] = useState<string | null>(null)

  const remove = async (row: WebhookSubscription) => {
    setError(null)
    const consequence = isRelayDelivery(row)
      ? 'OtoDock stops forwarding its events to this install.'
      : 'This unregisters the webhook at the vendor.'
    if (!(await dialogs.confirm({
      title: `Delete the subscription for ${row.vendor_target}?`,
      body: consequence,
      confirmLabel: 'Delete',
      tone: 'danger',
    }))) {
      return
    }
    // The row goes even when the vendor keeps the registration
    // (orphan-tolerant); that outcome is the one thing worth saying.
    const report = (res: { vendor_detached?: boolean }) => {
      if (res.vendor_detached === false) {
        setError(
          `The subscription is deleted but the webhook at the vendor could not be removed. Delete it by hand in the vendor's settings for ${row.vendor_target}.`,
        )
      }
    }
    try {
      report(await del.mutateAsync({ id: row.id }))
      return
    } catch (err) {
      const linked = linkedTriggersOf(err)
      if (linked.length === 0) {
        setError((err as Error).message)
        return
      }
      const names = linked.map((t) => t.name).join(', ')
      const one = linked.length === 1
      if (!(await dialogs.confirm({
        title: 'Delete it anyway?',
        body: `${linked.length} trigger${one ? '' : 's'} still fire${one ? 's' : ''} from this subscription: ${names}. The trigger${one ? ' stays' : 's stay'} but no longer receive${one ? 's' : ''} events.`,
        confirmLabel: 'Delete anyway',
        tone: 'danger',
      }))) {
        return
      }
    }
    try {
      report(await del.mutateAsync({ id: row.id, force: true }))
    } catch (err) {
      setError((err as Error).message)
    }
  }

  return { remove, isPending: del.isPending, error, setError }
}
