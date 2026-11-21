/**
 * A machine's card while install ahead works: the tools it is installing,
 * each one that could not be installed with its cause behind the info tip,
 * nothing once everything is installed; the list is read every 5 s while a
 * machine installs.
 */
import { describe, expect, it } from 'vitest'
import { render, screen } from '@testing-library/react'
import MachineToolsLine from '../components/MachineToolsLine'
import { machineToolsSentence } from '../lib/status/machineTools'
import { toolsAheadRefetch, type RemoteMachine } from '../api/remoteMachines'

const machine = (tools: RemoteMachine['tools_ahead']): RemoteMachine =>
  ({ id: 'm1', name: 'Laptop', status: 'online', tools_ahead: tools } as unknown as RemoteMachine)

describe('MachineToolsLine', () => {
  it('names the tools a machine is installing', () => {
    render(<MachineToolsLine machine={machine({ installing: ['GitHub', 'Linear'], failed: {} })} />)
    expect(screen.getByText('Installing 2 tools: GitHub and Linear.')).toBeInTheDocument()
  })

  it('says which tool could not be installed, its cause behind the tip', () => {
    render(<MachineToolsLine machine={machine({ installing: [], failed: { Linear: 'no wheel for this Python' } })} />)
    expect(screen.getByTestId('machine-tools-line')).toHaveTextContent('Linear could not be installed. Chats there may start without it.')
    expect(screen.queryByText('no wheel for this Python')).not.toBeInTheDocument()
  })

  it('says nothing once everything is installed', () => {
    const { container } = render(<MachineToolsLine machine={machine(null)} />)
    expect(container).toBeEmptyDOMElement()
  })

  it('counts one tool in words', () => {
    expect(machineToolsSentence(['GitHub'])).toBe('Installing GitHub.')
    expect(machineToolsSentence(['A', 'B', 'C'])).toBe('Installing 3 tools: A, B and C.')
    // After a release the sign-in installs many: the card names three.
    expect(machineToolsSentence(['Checks', 'Delegation', 'Display', 'Memory', 'Schedules']))
      .toBe('Installing 5 tools: Checks, Delegation, Display and 2 more.')
  })

  it('reads the list every 5 s while a machine installs, else every 15 s', () => {
    expect(toolsAheadRefetch([machine({ installing: ['GitHub'], failed: {} })])).toBe(5000)
    expect(toolsAheadRefetch([machine({ installing: [], failed: { A: 'x' } }), machine(null)])).toBe(15000)
    expect(toolsAheadRefetch(undefined)).toBe(15000)
  })
})
