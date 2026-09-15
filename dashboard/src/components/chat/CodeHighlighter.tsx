import { Prism as SyntaxHighlighter } from 'react-syntax-highlighter'
import { vscDarkPlus } from 'react-syntax-highlighter/dist/esm/styles/prism'
import { CODE_BODY_STYLE } from './CodeBlock'

interface Props {
  language: string
  children: string
}

// The highlighter proper: react-syntax-highlighter's Prism build with every
// grammar, so any language a model emits gets colours. Reached only through
// CodeBlock's lazy boundary, on the first fenced block a chat renders.
export default function CodeHighlighter({ language, children }: Props) {
  return (
    <SyntaxHighlighter
      language={language}
      style={vscDarkPlus}
      customStyle={CODE_BODY_STYLE}
      showLineNumbers={false}
      wrapLongLines={false}
    >
      {children}
    </SyntaxHighlighter>
  )
}
