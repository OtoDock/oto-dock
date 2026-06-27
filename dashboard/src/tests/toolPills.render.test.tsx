import { describe, it, expect } from 'vitest'
import { render, screen, fireEvent } from '@testing-library/react'

import ToolActivity from '@/components/chat/ToolActivity'
import SubagentInfo from '@/components/chat/SubagentInfo'
import BgCommandInfo from '@/components/chat/BgCommandInfo'
import DelegateTaskInfo from '@/components/chat/DelegateTaskInfo'

describe('ToolActivity — Bash pill', () => {
  it('collapsed shows the description; expanding reveals the command', () => {
    render(
      <ToolActivity
        name="Bash"
        summary="grep -rn foo proxy/ | head"
        status="done"
        toolInput={{ command: 'grep -rn foo proxy/ | head', description: 'Search for foo in proxy' }}
        resultSummary="ok"
      />,
    )
    expect(screen.getByText('Search for foo in proxy')).toBeTruthy()
    expect(screen.queryByText('grep -rn foo proxy/ | head')).toBeNull()
    fireEvent.click(screen.getByText('Search for foo in proxy'))
    expect(screen.getByText('grep -rn foo proxy/ | head')).toBeTruthy()
  })

  it('expanding reveals the Output section when toolResult is present (codex parity)', () => {
    render(
      <ToolActivity
        name="Bash"
        status="done"
        toolInput={{ command: 'printf alpha; printf beta' }}
        toolResult={'alpha\nbeta'}
        resultSummary="2 lines"
      />,
    )
    expect(screen.getByText('2 lines')).toBeTruthy()
    expect(screen.queryByText('Output')).toBeNull()
    fireEvent.click(screen.getByText('printf alpha; printf beta', { selector: 'span' }))
    expect(screen.getByText('Output')).toBeTruthy()
    expect(screen.getByText(/alpha\s*beta/)).toBeTruthy()
  })

  it('expanding un-truncates the description title; the command fallback stays clipped', () => {
    const { rerender } = render(
      <ToolActivity
        name="Bash"
        status="done"
        toolInput={{ command: 'ls', description: 'A long description of what this does' }}
      />,
    )
    const title = () => screen.getByText('A long description of what this does')
    expect(title().className).toContain('truncate')
    fireEvent.click(title())
    expect(title().className).toContain('whitespace-normal')
    expect(title().className).not.toContain('truncate')
    // No description (e.g. Codex): the title IS the command — expanding keeps
    // it clipped, the body already shows the command verbatim.
    rerender(<ToolActivity name="Bash" status="done" toolInput={{ command: 'pwd && date' }} />)
    const cmdTitle = screen.getByText('pwd && date', { selector: 'span' })
    expect(cmdTitle.className).toContain('truncate')
    fireEvent.click(cmdTitle)
    expect(cmdTitle.className).toContain('truncate')
  })
})

describe('SubagentInfo — expandable agent pill', () => {
  it('expands to the prompt and the foreground report', () => {
    render(
      <SubagentInfo
        description="map the satellite code"
        subagentType="Explore"
        isActive={false}
        toolInput={{ prompt: 'Explore the repo at /x and report the hook flow.' }}
        toolResult="The hook flow is: settings.json → permission_gate.py → proxy."
      />,
    )
    expect(screen.queryByText(/Explore the repo at/)).toBeNull()
    fireEvent.click(screen.getByText('map the satellite code'))
    expect(screen.getByText(/Explore the repo at/)).toBeTruthy()
    expect(screen.getByText(/The hook flow is/)).toBeTruthy()
  })

  it('stays a plain pill when there is nothing to expand (old rows)', () => {
    const { container } = render(
      <SubagentInfo description="legacy row" subagentType="general-purpose" isActive={false} />,
    )
    expect(container.querySelector('.cursor-pointer')).toBeNull()
  })
})

describe('BgCommandInfo — merged background-command pill', () => {
  it('shows the description collapsed and the paired command + output expanded', () => {
    render(
      <BgCommandInfo
        command="npm run build"
        description="Build the dashboard"
        isActive={false}
        toolInput={{ command: 'npm run build', run_in_background: true }}
        toolResult="Command running in background with ID bash_7"
      />,
    )
    expect(screen.getByText('Build the dashboard')).toBeTruthy()
    expect(screen.queryByText('npm run build')).toBeNull()
    fireEvent.click(screen.getByText('Build the dashboard'))
    expect(screen.getByText('npm run build')).toBeTruthy()
    expect(screen.getByText(/bash_7/)).toBeTruthy()
  })

  it('old-proxy rows (command == description twin) expand to the PAIRED command, never the description again', () => {
    render(
      <BgCommandInfo
        command="Build the dashboard"
        description="Build the dashboard"
        isActive={false}
        toolInput={{ command: 'npm run build', run_in_background: true }}
      />,
    )
    fireEvent.click(screen.getByText('Build the dashboard'))
    expect(screen.getByText('npm run build')).toBeTruthy()
    // exactly one occurrence — the collapsed label; not repeated in the body
    expect(screen.getAllByText('Build the dashboard')).toHaveLength(1)
  })

  it('an unpaired old-proxy row has nothing real to expand to', () => {
    const { container } = render(
      <BgCommandInfo
        command="Build the dashboard"
        description="Build the dashboard"
        isActive={false}
      />,
    )
    expect(container.querySelector('.cursor-pointer')).toBeNull()
  })

  it('the bash badge is crush-proof — flex cannot wrap it mid-word on narrow screens', () => {
    render(
      <BgCommandInfo command="sleep 10" description="A long description that squeezes the row" isActive />,
    )
    expect(screen.getByText('bash').className).toContain('shrink-0')
  })
})

describe('DelegateTaskInfo — expandable delegate pill', () => {
  it('expands to the full prompt when present', () => {
    render(
      <DelegateTaskInfo
        taskName="triage inbox"
        agent="support-bot"
        promptPreview="Please triage…"
        status="completed"
        prompt="Please triage the attached report and summarize the top issues."
      />,
    )
    expect(screen.queryByText(/summarize the top issues/)).toBeNull()
    fireEvent.click(screen.getByText('triage inbox'))
    expect(screen.getByText(/summarize the top issues/)).toBeTruthy()
  })

  it('is not expandable on preview-only legacy rows', () => {
    const { container } = render(
      <DelegateTaskInfo taskName="old row" agent="a" promptPreview="short…" status="completed" />,
    )
    expect(container.querySelector('.cursor-pointer')).toBeNull()
  })

  it('renders the agent badge on both breakpoint rows, crush-proof', () => {
    render(
      <DelegateTaskInfo taskName="lane" agent="support-bot" promptPreview="p…" status="completed" />,
    )
    const badges = screen.getAllByText('support-bot')
    expect(badges).toHaveLength(2) // sm+ inline + mobile row
    for (const b of badges) expect(b.className).toContain('shrink-0')
  })
})
