import { useSyncExternalStore } from 'react'

import AlertDialog, { PromptDialog } from './AlertDialog'
import {
  DIALOG_KIND, answerAlert, answerConfirm, answerPrompt, dialogRequests, subscribeDialogs, type DialogRequest,
} from './dialogs'

function Hosted({ request: r }: { request: DialogRequest }) {
  switch (r.kind) {
    case DIALOG_KIND.CONFIRM:
      return (
        <AlertDialog
          title={r.options.title}
          tone={r.options.tone}
          confirmLabel={r.options.confirmLabel}
          cancelLabel={r.options.cancelLabel}
          onConfirm={() => answerConfirm(r.id, true)}
          onCancel={() => answerConfirm(r.id, false)}
        >
          {r.options.body}
        </AlertDialog>
      )
    case DIALOG_KIND.ALERT:
      return (
        <AlertDialog
          title={r.options.title}
          hideCancel
          confirmLabel={r.options.okLabel ?? 'OK'}
          onConfirm={() => answerAlert(r.id)}
          onCancel={() => answerAlert(r.id)}
        >
          {r.options.body}
        </AlertDialog>
      )
    case DIALOG_KIND.PROMPT:
      return (
        <PromptDialog
          {...r.options}
          onSubmit={(value) => answerPrompt(r.id, value)}
          onCancel={() => answerPrompt(r.id, null)}
        />
      )
  }
}

/** The dialogs `useDialogs()` asks for, newest on top. Mounted once, in App. */
export default function DialogsHost() {
  const requests = useSyncExternalStore(subscribeDialogs, dialogRequests, dialogRequests)
  return <>{requests.map((r) => <Hosted key={r.id} request={r} />)}</>
}
