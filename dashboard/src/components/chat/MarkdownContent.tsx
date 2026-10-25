import { memo, useEffect, useRef, useState, isValidElement, cloneElement } from 'react'
import ReactMarkdown, { defaultUrlTransform } from 'react-markdown'
import remarkGfm from 'remark-gfm'
import type { Components } from 'react-markdown'
import { resolveChatPath, type ResolvedChatPath } from '../../api/chats'
import CodeBlock from './CodeBlock'
import ChatFilePreview from './ChatFilePreview'

import SearchHighlight from './SearchHighlight'

import { isFileSystemPathHref } from '../../lib/chat/markdownLinks'
import { useChatFileContext } from '../../lib/chat/chatFileContext'
import { useSearch } from '../../contexts/useSearch'

// micromark percent-encodes non-ASCII hrefs (Greek filenames); a raw '%' in
// a path would make decodeURI throw.
function decodePathHref(href: string): string {
  try {
    return decodeURI(href)
  } catch {
    return href
  }
}

// defaultUrlTransform empties any href outside its protocol allowlist —
// C:\..., file://, sandbox: hrefs would reach the `a` component as "" (an
// anchor that reloads the current route). Path hrefs pass through verbatim;
// everything else keeps the javascript:/data: neutralization.
function urlTransform(url: string): string | null | undefined {
  return isFileSystemPathHref(url) ? url : defaultUrlTransform(url)
}

// How long the chip's transient "not found" state lasts before reverting.
const NOT_FOUND_REVERT_MS = 2500

/**
 * A filesystem-path chip. Without `ChatFileContext` (meetings, every other
 * MarkdownContent surface) it is exactly the inert chip. Inside a chat it
 * becomes a button: click → `POST /v1/chats/{id}/resolve-path` → on a match
 * the workspace preview stack opens (`ChatFilePreview`); `found: false` or a
 * fetch error shows a transient "not found" state (no toast) that reverts —
 * negatives are not cached, so the next click re-resolves.
 */
function FileChip({ href }: { href: string }) {
  const ctx = useChatFileContext()
  const [busy, setBusy] = useState(false)
  const [notFound, setNotFound] = useState(false)
  const [preview, setPreview] = useState<ResolvedChatPath | null>(null)
  const revertTimer = useRef<ReturnType<typeof setTimeout> | null>(null)
  useEffect(() => () => {
    if (revertTimer.current) clearTimeout(revertTimer.current)
  }, [])

  // micromark percent-encodes non-ASCII/backslash hrefs. The decoded form is
  // what the user sees AND what gets POSTed — the backend never URL-decodes.
  const decoded = decodePathHref(href)

  if (!ctx) {
    return (
      <span
        title="A local file path. Ask the agent for a preview or a download link."
        className="inline-flex items-center gap-1 px-1.5 py-0.5 rounded-sm bg-surface-muted text-primary text-[0.85em] font-mono cursor-default break-all"
      >
        <span aria-hidden="true">📄</span>
        {decoded}
      </span>
    )
  }

  const handleClick = async () => {
    if (busy) return
    if (revertTimer.current) {
      clearTimeout(revertTimer.current)
      revertTimer.current = null
    }
    setNotFound(false)
    setBusy(true)
    let resolved: ResolvedChatPath | null
    try {
      resolved = await resolveChatPath(ctx.chatId, decoded)
    } catch {
      resolved = null // network/HTTP error — same transient state as found:false
    }
    setBusy(false)
    if (resolved) {
      setPreview(resolved)
      return
    }
    setNotFound(true)
    revertTimer.current = setTimeout(() => setNotFound(false), NOT_FOUND_REVERT_MS)
  }

  return (
    <>
      <button
        type="button"
        onClick={handleClick}
        title={notFound ? "File not found in the agent's workspace" : 'Open file preview'}
        className={`inline-flex items-center gap-1 px-1.5 py-0.5 rounded-sm bg-surface-muted text-primary text-[0.85em] font-mono cursor-pointer hover:ring-1 hover:ring-action break-all text-left align-baseline${busy ? ' opacity-60' : ''}`}
      >
        <span aria-hidden="true">📄</span>
        {decoded}
        {notFound ? (
          <span className="font-sans text-status-warning-fg whitespace-nowrap">Not found</span>
        ) : (
          <span aria-hidden="true" className="opacity-60">↗</span>
        )}
      </button>
      {preview && <ChatFilePreview resolved={preview} onClose={() => setPreview(null)} />}
    </>
  )
}

function headerCell({ children }: { children?: React.ReactNode }) {
  return (
    <th className="px-3 py-2 text-left text-xs font-semibold text-secondary uppercase tracking-wider whitespace-nowrap">
      {children}
    </th>
  )
}

function dataCell({ children }: { children?: React.ReactNode }) {
  return (
    <td className="px-3 py-2 text-primary border-t border-subtle whitespace-nowrap">
      {children}
    </td>
  )
}

const baseComponents: Components = {
  // Code blocks + inline code
  code({ className, children }) {
    const match = /language-(\w+)/.exec(className || '')
    const text = String(children).replace(/\n$/, '')

    // Detect block vs inline: if parent is <pre>, it's a block
    // react-markdown v10: block code is always inside <pre>
    // We check via the node prop or by checking if inline
    const isInline = !className && !text.includes('\n')

    if (isInline) {
      return <CodeBlock inline>{text}</CodeBlock>
    }

    return <CodeBlock language={match?.[1]}>{text}</CodeBlock>
  },

  // Override pre to be a passthrough (CodeBlock handles its own wrapper)
  pre({ children }) {
    return <>{children}</>
  },

  // Links — styled with external icon
  a({ href, children }) {
    // Filesystem paths render as chips (clickable only inside a chat) —
    // same predicate as urlTransform
    if (href && isFileSystemPathHref(href)) {
      return <FileChip href={href} />
    }
    const isExternal = href?.startsWith('http')
    return (
      <a
        href={href}
        target={isExternal ? '_blank' : undefined}
        rel={isExternal ? 'noopener noreferrer' : undefined}
        className="text-link hover:text-link-hover hover:underline underline-offset-2 inline-flex items-center gap-0.5"
      >
        {children}
        {isExternal && (
          <svg className="w-3 h-3 inline-block shrink-0 opacity-60" fill="none" stroke="currentColor" viewBox="0 0 24 24">
            <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M10 6H6a2 2 0 00-2 2v10a2 2 0 002 2h10a2 2 0 002-2v-4M14 4h6m0 0v6m0-6L10 14" />
          </svg>
        )}
      </a>
    )
  },

  // Tables — scrollable wrapper
  table({ children }) {
    return (
      <div className="overflow-x-auto my-3 rounded-card border border-subtle">
        <table className="min-w-full divide-y divide-subtle text-sm">
          {children}
        </table>
      </div>
    )
  },

  thead({ children }) {
    return <thead className="bg-surface-muted">{children}</thead>
  },

  th: headerCell,

  td: dataCell,

  tr({ children }) {
    return <tr className="even:bg-surface-muted/40">{children}</tr>
  },
}

/** Recursively walk React children and wrap string nodes with SearchHighlight. */
function highlightChildren(
  children: React.ReactNode,
  idPrefix: string,
  counterRef: { current: number },
  order: number,
): React.ReactNode {
  if (typeof children === 'string') {
    const id = `${idPrefix}-${counterRef.current++}`
    return <SearchHighlight text={children} matchId={id} order={order} />
  }
  if (Array.isArray(children)) {
    return children.map((child, i) => {
      if (typeof child === 'string') {
        const id = `${idPrefix}-${counterRef.current++}`
        return <SearchHighlight key={i} text={child} matchId={id} order={order} />
      }
      if (isValidElement<{ children?: React.ReactNode }>(child)) {
        const childProps = child.props
        if (childProps?.children) {
          return cloneElement(child, {
            ...childProps,
            key: child.key ?? i,
            children: highlightChildren(childProps.children, idPrefix, counterRef, order),
          })
        }
      }
      return child
    })
  }
  if (isValidElement<{ children?: React.ReactNode }>(children)) {
    const p = children.props
    if (p?.children) {
      return cloneElement(children, {
        ...p,
        children: highlightChildren(p.children, idPrefix, counterRef, order),
      })
    }
  }
  return children
}

type WrappedTag = 'p' | 'li' | 'td' | 'th' | 'strong' | 'em' | 'h1' | 'h2' | 'h3' | 'h4' | 'blockquote'
type WrappedProps = React.HTMLAttributes<HTMLElement> & { node?: unknown }

// The wrapped tags that baseComponents renders itself (the table cells).
const BASE_RENDERERS: Partial<Record<WrappedTag, (props: { children?: React.ReactNode }) => React.ReactNode>> = {
  th: headerCell,
  td: dataCell,
}

/** Build components with search highlighting injected into text-bearing elements. */
function buildSearchComponents(idPrefix: string, counterRef: { current: number }, order: number): Components {
  const wrap = (Tag: WrappedTag) =>
    function WrappedElement({ children, ...props }: WrappedProps) {
      // Use the base renderer where the tag has one, otherwise the raw tag
      const base = BASE_RENDERERS[Tag]
      if (base) {
        return base({ ...props, children: highlightChildren(children, idPrefix, counterRef, order) })
      }
      const El = Tag
      return <El {...props}>{highlightChildren(children, idPrefix, counterRef, order)}</El>
    }

  return {
    ...baseComponents,
    p: wrap('p'),
    li: wrap('li'),
    td: wrap('td'),
    th: wrap('th'),
    strong: wrap('strong'),
    em: wrap('em'),
    h1: wrap('h1'),
    h2: wrap('h2'),
    h3: wrap('h3'),
    h4: wrap('h4'),
    blockquote: wrap('blockquote'),
  }
}

interface Props {
  children: string
  className?: string
  searchMatchIdPrefix?: string  // stable prefix for search match IDs
  searchOrder?: number          // explicit sort key for match ordering
}

// memo: ReactMarkdown re-runs the full unified parse on EVERY render and
// the chat maps messages inline — without this, each turn-end state write
// re-parses every message in the chat (the main-thread burst that starved
// the duplex player's 250ms lookahead). Props are all primitives, so the
// default shallow compare is exact; context-driven re-renders (search)
// still pass through memo as always.
function MarkdownContent({ children, className, searchMatchIdPrefix, searchOrder }: Props) {
  const { query } = useSearch()
  // A fresh counter each render, so the IDs are stable for the same content
  const counter = { current: 0 }

  const idPrefix = searchMatchIdPrefix || 'md'
  const order = searchOrder ?? 0
  const components = query
    ? buildSearchComponents(idPrefix, counter, order)
    : baseComponents

  return (
    <div className={`markdown-content prose prose-sm max-w-none
      prose-headings:mt-4 prose-headings:mb-2 prose-headings:font-semibold prose-h1:font-display prose-h2:font-display
      prose-p:my-2 prose-p:leading-relaxed
      prose-ul:my-2 prose-ol:my-2 prose-li:my-0.5
      prose-blockquote:border-l-strong prose-blockquote:bg-surface-muted prose-blockquote:py-1 prose-blockquote:px-3 prose-blockquote:rounded-r-card prose-blockquote:not-italic
      prose-hr:my-4
      prose-strong:text-primary
      ${className || ''}`}
    >
      <ReactMarkdown remarkPlugins={[remarkGfm]} components={components} urlTransform={urlTransform}>
        {children}
      </ReactMarkdown>
    </div>
  )
}

export default memo(MarkdownContent)
