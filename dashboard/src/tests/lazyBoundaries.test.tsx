// The code highlighter and the password scorer are lazy chunks. Pins the
// contract of each boundary: what shows before the chunk resolves and what
// replaces it after.
import { render, screen, waitFor } from '@testing-library/react'
import { describe, expect, it } from 'vitest'
import CodeBlock from '../components/chat/CodeBlock'
import PasswordStrengthBar from '../components/PasswordStrengthBar'

describe('lazy boundaries', () => {
  it('a code block shows its text at once and highlights once the chunk resolves', async () => {
    render(<CodeBlock language="python">{'print("hello")'}</CodeBlock>)
    // Fallback: the same text in a plain <pre>, before any chunk resolves.
    const fallback = screen.getByText('print("hello")')
    expect(fallback.closest('pre')).not.toBeNull()
    expect(screen.getByText('python')).toBeInTheDocument()
    // Highlighter: Prism tokenises into a language-classed <code>. The chunk
    // evaluates every grammar, which takes seconds in a loaded worker.
    await waitFor(
      () => expect(document.querySelector('code[class*="language-python"]')).not.toBeNull(),
      { timeout: 15_000 })
  }, 20_000)

  it('inline code never touches the highlighter', () => {
    const { container } = render(<CodeBlock inline>{'ls -la'}</CodeBlock>)
    expect(container.querySelector('code')?.textContent).toBe('ls -la')
    expect(container.querySelector('pre')).toBeNull()
  })

  it('the password meter renders nothing until there is a password, then a scored label', async () => {
    const { container, rerender } = render(<PasswordStrengthBar password="" />)
    expect(container).toBeEmptyDOMElement()
    rerender(<PasswordStrengthBar password="lamp-mountain-92-Tr0ub4dor" />)
    // Any of the five labels proves the scorer chunk resolved and ran.
    expect(await screen.findByText(/^(Very weak|Weak|Fair|Strong|Very strong)$/)).toBeInTheDocument()
  })

  it('a short password is scored without the dictionary verdict', async () => {
    render(<PasswordStrengthBar password="abc" />)
    expect(await screen.findByText('At least 8 characters required')).toBeInTheDocument()
  })
})
