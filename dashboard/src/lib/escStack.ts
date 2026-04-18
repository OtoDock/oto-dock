/**
 * Escape-key precedence stack.
 *
 * Several panels and portals (FindBar, PlanPanel, TodoPanel, file previews,
 * the workspace overlay…) all want to handle Escape. With each one binding
 * its own `keydown` listener directly to `document`, every visible handler
 * fires on a single keypress, which collapses unrelated panels together.
 *
 * `pushEscHandler(handler)` registers a handler on a LIFO stack and returns
 * a pop function. Only the topmost handler is invoked per Escape press.
 * Components call this in a `useEffect` and pop on unmount.
 */

type EscHandler = () => void

const stack: EscHandler[] = []
let installed = false

function onKeyDown(e: KeyboardEvent) {
  if (e.key !== 'Escape') return
  const top = stack[stack.length - 1]
  if (!top) return
  top()
}

function install() {
  if (installed) return
  document.addEventListener('keydown', onKeyDown)
  installed = true
}

export function pushEscHandler(handler: EscHandler): () => void {
  install()
  stack.push(handler)
  return () => {
    const i = stack.lastIndexOf(handler)
    if (i >= 0) stack.splice(i, 1)
  }
}
