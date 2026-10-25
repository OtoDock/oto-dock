// An integration's group on Integrations: the add button is the group's
// primary action and its label never repeats the word "account".
import { fireEvent, render, screen } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'

import type { Integration } from '../api/credentials'
import { AccountsManager } from '../components/accounts/AccountsManager'
import type { AccountsOps } from '../components/accounts/types'
import { BUTTON_VARIANT } from '../components/ui/classes'

vi.mock('../components/accounts/AccountCard', () => ({
  AccountCard: ({ account }: { account: { account_label: string } }) => <div data-testid="account">{account.account_label}</div>,
}))
vi.mock('../components/accounts/AccountForm', () => ({
  AccountForm: () => <div data-testid="account-form" />,
}))

function integration(accounts: string[], multi = true): Integration {
  return {
    mcp_name: 'github-mcp', display_name: 'GitHub Account', description: '', configured: accounts.length > 0,
    required_keys: [], fields: [], supports_multi_account: multi, candidate_agents: [],
    accounts: accounts.map((account_label) => ({ account_label })),
  } as unknown as Integration
}

function open(accounts: string[], multi = true) {
  render(<AccountsManager integration={integration(accounts, multi)} ops={{} as AccountsOps} />)
  fireEvent.click(screen.getByRole('button', { name: 'GitHub Account' }))
}

describe('AccountsManager', () => {
  it('offers "Add an account" as the primary action when none is connected', () => {
    open([])
    const add = screen.getByRole('button', { name: 'Add an account' })
    expect(add.className).toContain(BUTTON_VARIANT.primary)
    expect(screen.queryByText(/account account/i)).toBeNull()
  })

  it('offers "Add another account" beside the connected ones, and opens the form', () => {
    open(['work'])
    expect(screen.getByTestId('account')).toHaveTextContent('work')
    const add = screen.getByRole('button', { name: 'Add another account' })
    expect(add.className).toContain(BUTTON_VARIANT.primary)
    fireEvent.click(add)
    expect(screen.getByTestId('account-form')).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Add another account' })).toBeNull()
  })

  it('offers no second account on an integration that takes one', () => {
    open(['work'], false)
    expect(screen.queryByRole('button', { name: /^Add/ })).toBeNull()
  })
})
