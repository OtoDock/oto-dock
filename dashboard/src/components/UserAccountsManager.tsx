/**
 * UserAccountsManager — per-user multi-account card.
 *
 * Thin wrapper around the shared ``AccountsManager`` that injects the
 * user-side mutation hooks via the ``ops`` bundle. See
 * ``components/accounts/`` for the shared rendering layer.
 */

import type { Integration } from '../api/credentials'
import {
  useSetIntegration,
  useDeleteIntegration,
  useSetDefaultAccount,
  useSetAccountAgentBinding,
  useRemoveAccountAgentBinding,
} from '../api/credentials'
import { AccountsManager } from './accounts/AccountsManager'
import type { AccountsOps } from './accounts/types'

interface Props {
  integration: Integration
}

export function UserAccountsManager({ integration }: Props) {
  const ops: AccountsOps = {
    setIntegration: useSetIntegration(),
    deleteIntegration: useDeleteIntegration(),
    setDefault: useSetDefaultAccount(),
    setBinding: useSetAccountAgentBinding(),
    removeBinding: useRemoveAccountAgentBinding(),
  }
  return <AccountsManager integration={integration} ops={ops} />
}
