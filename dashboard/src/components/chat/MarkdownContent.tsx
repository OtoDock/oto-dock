import { useRef, isValidElement, cloneElement } from 'react'
import ReactMarkdown from 'react-markdown'
import remarkGfm from 'remark-gfm'
import type { Components } from 'react-markdown'
import CodeBlock from './CodeBlock'
import SearchHighlight from './SearchHighlight'
import { useSearch } from '../../contexts/SearchContext'

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
    const isExternal = href?.startsWith('http')
    return (
      <a
        href={href}
        target={isExternal ? '_blank' : undefined}
        rel={isExternal ? 'noopener noreferrer' : undefined}
        className="text-blue-600 dark:text-blue-400 hover:text-blue-800 dark:hover:text-blue-300 hover:underline underline-offset-2 inline-flex items-center gap-0.5"
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
      <div className="overflow-x-auto my-3 rounded-lg border border-p-border-light">
        <table className="min-w-full divide-y divide-p-border-light text-sm">
          {children}
        </table>
      </div>
    )
  },

  thead({ children }) {
    return <thead className="bg-brand-50">{children}</thead>
  },

  th({ children }) {
    return (
      <th className="px-3 py-2 text-left text-xs font-semibold text-brand uppercase tracking-wider whitespace-nowrap">
        {children}
      </th>
    )
  },

  td({ children }) {
    return (
      <td className="px-3 py-2 text-p-text border-t border-p-border-light whitespace-nowrap">
        {children}
      </td>
    )
  },

  tr({ children }) {
    return <tr className="even:bg-p-surface/40">{children}</tr>
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
      if (isValidElement(child)) {
        const childProps = child.props as any
        if (childProps?.children) {
          return cloneElement(child as React.ReactElement<any>, {
            ...childProps,
            key: child.key ?? i,
            children: highlightChildren(childProps.children, idPrefix, counterRef, order),
          })
        }
      }
      return child
    })
  }
  if (isValidElement(children)) {
    const p = (children as any).props
    if (p?.children) {
      return cloneElement(children as React.ReactElement<any>, {
        ...p,
        children: highlightChildren(p.children, idPrefix, counterRef, order),
      })
    }
  }
  return children
}

/** Build components with search highlighting injected into text-bearing elements. */
function buildSearchComponents(idPrefix: string, counterRef: { current: number }, order: number): Components {
  const wrap = (Tag: string) =>
    function WrappedElement({ children, ...props }: any) {
      // Use base component if it exists, otherwise use raw tag
      const base = (baseComponents as any)[Tag]
      if (base) {
        return base({ ...props, children: highlightChildren(children, idPrefix, counterRef, order) })
      }
      const El = Tag as any
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

export default function MarkdownContent({ children, className, searchMatchIdPrefix, searchOrder }: Props) {
  const { query } = useSearch()
  const counterRef = useRef(0)
  // Reset counter on each render so IDs are stable for same content
  counterRef.current = 0

  const idPrefix = searchMatchIdPrefix || 'md'
  const order = searchOrder ?? 0
  const components = query
    ? buildSearchComponents(idPrefix, counterRef, order)
    : baseComponents

  return (
    <div className={`markdown-content prose prose-sm max-w-none dark:prose-invert
      prose-headings:mt-4 prose-headings:mb-2 prose-headings:font-semibold
      prose-p:my-2 prose-p:leading-relaxed
      prose-ul:my-2 prose-ol:my-2 prose-li:my-0.5
      prose-blockquote:border-l-blue-400 prose-blockquote:bg-blue-50 dark:prose-blockquote:bg-blue-900/20 prose-blockquote:py-1 prose-blockquote:px-3 prose-blockquote:rounded-r-lg prose-blockquote:not-italic
      prose-hr:my-4
      prose-strong:text-gray-900 dark:prose-strong:text-gray-100
      ${className || ''}`}
    >
      <ReactMarkdown remarkPlugins={[remarkGfm]} components={components}>
        {children}
      </ReactMarkdown>
    </div>
  )
}
