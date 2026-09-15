import { describe, it, expect } from 'vitest'
import { render, fireEvent } from '@testing-library/react'

import McpIcon from '@/components/McpIcon'

// ─── McpIcon: the proxy-served image with the first-letter tile as fallback ───

describe('McpIcon', () => {
  it('loads the image for a bundled MCP that ships an icon', () => {
    const { container } = render(<McpIcon name="memory-mcp" label="Memory" category="core" hasIcon />)
    const img = container.querySelector('img')!
    expect(img.getAttribute('src')).toBe('/v1/mcps/memory-mcp/icon.png')
    expect(img.getAttribute('alt')).toBe('')
  })

  it('draws the letter tile for a bundled MCP without an icon', () => {
    const { container, getByText } = render(<McpIcon name="ssh-hosts" label="SSH Hosts" category="custom" />)
    expect(container.querySelector('img')).toBeNull()
    expect(getByText('S')).toBeTruthy()
  })

  it("draws OtoDock's own community mark without the white frame", () => {
    const { container } = render(<McpIcon name="camoufox" label="Browser (Camoufox)" category="community" author="OtoDock" />)
    const img = container.querySelector('img')!
    expect(img.getAttribute('src')).toBe('/v1/mcps/camoufox/icon.png')
    expect(img.parentElement!.className).not.toContain('bg-white')
  })

  it('always tries the image for a community MCP and falls back on error', () => {
    const { container, getByText } = render(<McpIcon name="github-mcp" label="GitHub" category="community" author="GitHub" />)
    const img = container.querySelector('img')!
    expect(img.getAttribute('src')).toBe('/v1/mcps/github-mcp/icon.png')
    // Community marks sit on a white tile so a black mark stays visible on the dark theme.
    expect(img.parentElement!.className).toContain('bg-white')
    fireEvent.error(img)
    expect(container.querySelector('img')).toBeNull()
    expect(getByText('G')).toBeTruthy()
  })
})
