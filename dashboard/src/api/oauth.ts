import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { apiFetch } from './auth'

/**
 * Generic OAuth provider hooks — work for any provider declared in an
 * MCP's manifest. Provider examples: "google" (workspace-mcp), "linear",
 * "notion", "slack", "github".
 */

export interface OAuthAccountsResponse {
  accounts: import('./credentials').AccountSummary[]
  has_service_credentials_only: boolean
}

/**
 * Per-(provider, mcp) accounts list. Used by OAuthAccountForm to gate
 * service rows where ``service.requires_user_oauth=true`` AND admin has
 * S2S-only credentials configured (no user-OAuth path).
 */
export const useOAuthAccounts = (provider: string, mcpName: string) =>
  useQuery({
    queryKey: ['oauth-accounts', provider, mcpName],
    queryFn: async (): Promise<OAuthAccountsResponse> => {
      const res = await apiFetch(
        `/v1/oauth/${provider}/accounts?mcp_name=${encodeURIComponent(mcpName)}`,
      )
      if (!res.ok) throw new Error('Failed to load OAuth accounts')
      return res.json()
    },
    enabled: !!provider && !!mcpName,
  })

export interface OAuthStartParams {
  provider: string
  mcpName: string
  services: string[]
  accountLabel?: string
  isService?: boolean
  mobile?: boolean
}

export const useStartOAuth = () => {
  return useMutation({
    mutationFn: async (params: OAuthStartParams): Promise<{ url: string }> => {
      const res = await apiFetch(`/v1/oauth/${params.provider}/start`, {
        method: 'POST',
        body: JSON.stringify({
          mcp_name: params.mcpName,
          services: params.services,
          account_label: params.accountLabel || '',
          is_service: params.isService || false,
          mobile: params.mobile || false,
        }),
      })
      if (!res.ok) {
        const err = await res.json().catch(() => ({}))
        throw new Error(err.detail || 'Failed to start OAuth')
      }
      return res.json()
    },
  })
}

// ───────────────────────────────────────────────────────────────────
// Device-code + PAT flows
// ───────────────────────────────────────────────────────────────────

export interface DeviceCodeStartResult {
  device_code: string
  user_code: string
  verification_uri: string
  expires_in: number
  interval: number
  verification_uri_complete?: string // Microsoft sets this for one-click UX
}

export const useStartDeviceCode = () => {
  return useMutation({
    mutationFn: async (params: {
      provider: string
      mcpName: string
      services: string[]
      accountLabel?: string
      isService?: boolean
    }): Promise<DeviceCodeStartResult> => {
      const res = await apiFetch(
        `/v1/oauth/${params.provider}/device-code/start`,
        {
          method: 'POST',
          body: JSON.stringify({
            mcp_name: params.mcpName,
            services: params.services,
            account_label: params.accountLabel || '',
            is_service: params.isService || false,
          }),
        },
      )
      if (!res.ok) {
        const err = await res.json().catch(() => ({}))
        throw new Error(err.detail || 'Failed to start device-code flow')
      }
      return res.json()
    },
  })
}

export const usePollDeviceCode = () => {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: async (params: {
      provider: string
      mcpName: string
      deviceCode: string
      services: string[]
      accountLabel?: string
      isService?: boolean
    }): Promise<
      | { status: 'pending' }
      | { status: 'ok'; email: string; account_label: string }
    > => {
      const res = await apiFetch(
        `/v1/oauth/${params.provider}/device-code/poll`,
        {
          method: 'POST',
          body: JSON.stringify({
            mcp_name: params.mcpName,
            device_code: params.deviceCode,
            services: params.services,
            account_label: params.accountLabel || '',
            is_service: params.isService || false,
          }),
        },
      )
      if (res.status === 202) {
        return { status: 'pending' }
      }
      if (!res.ok) {
        const err = await res.json().catch(() => ({}))
        throw new Error(err.detail || 'Device-code poll failed')
      }
      return res.json()
    },
    onSuccess: (data, vars) => {
      if ('email' in data) {
        qc.invalidateQueries({
          queryKey: [vars.isService ? 'admin-integrations' : 'my-integrations'],
        })
      }
    },
  })
}

export const usePatSave = () => {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: async (params: {
      provider: string
      mcpName: string
      token: string
      services: string[]
      accountLabel?: string
      isService?: boolean
    }): Promise<{ status: string; email: string; account_label: string }> => {
      const res = await apiFetch(`/v1/oauth/${params.provider}/pat/save`, {
        method: 'POST',
        body: JSON.stringify({
          mcp_name: params.mcpName,
          token: params.token,
          services: params.services,
          account_label: params.accountLabel || '',
          is_service: params.isService || false,
        }),
      })
      if (!res.ok) {
        const err = await res.json().catch(() => ({}))
        throw new Error(err.detail || 'Failed to save token')
      }
      return res.json()
    },
    onSuccess: (_data, vars) => {
      qc.invalidateQueries({
        queryKey: [vars.isService ? 'admin-integrations' : 'my-integrations'],
      })
    },
  })
}

export const useDisconnectOAuth = () => {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: async ({
      provider,
      mcpName,
      accountLabel,
      isService = false,
    }: {
      provider: string
      mcpName: string
      accountLabel: string
      isService?: boolean
    }) => {
      const res = await apiFetch(`/v1/oauth/${provider}/disconnect`, {
        method: 'POST',
        body: JSON.stringify({
          mcp_name: mcpName,
          account_label: accountLabel,
          is_service: isService,
        }),
      })
      if (!res.ok) {
        const err = await res.json().catch(() => ({}))
        throw new Error(err.detail || 'Failed to disconnect')
      }
      return res.json()
    },
    onSuccess: (_data, vars) => {
      // Invalidate the right query depending on which scope we touched.
      qc.invalidateQueries({
        queryKey: [vars.isService ? 'admin-integrations' : 'my-integrations'],
      })
    },
  })
}

// ───────────────────────────────────────────────────────────────────
// Microsoft tenant-admin consent
// ───────────────────────────────────────────────────────────────────

/**
 * Start the Microsoft tenant-admin consent flow. Returns a URL that
 * opens at /{tenant}/v2.0/adminconsent — caller pops up the URL,
 * Microsoft posts back to /v1/oauth/microsoft/admin-consent/callback,
 * the callback renders a success page (web) or deep-link redirect
 * (mobile).
 *
 * Distinct from the standard authorize flow with `prompt=admin_consent`:
 * `prompt=admin_consent` only forces the consent UX for the user's home
 * tenant. The dedicated /adminconsent endpoint performs a tenant-wide
 * grant for every scope registered on the OAuth app.
 *
 * Requires MS_TENANT_ID infra credential to be configured — backend
 * rejects /common/ for admin-consent (Microsoft itself does too).
 */
export const useStartAdminConsent = () => {
  return useMutation({
    mutationFn: async (params: {
      mcpName: string
      mobile?: boolean
    }): Promise<{ url: string }> => {
      const res = await apiFetch(
        '/v1/oauth/microsoft/admin-consent/start',
        {
          method: 'POST',
          body: JSON.stringify({
            mcp_name: params.mcpName,
            mobile: params.mobile || false,
          }),
        },
      )
      if (!res.ok) {
        const err = await res.json().catch(() => ({}))
        throw new Error(err.detail || 'Failed to start admin-consent flow')
      }
      return res.json()
    },
  })
}

// ───────────────────────────────────────────────────────────────────
// Bearer-allowlist admin
// ───────────────────────────────────────────────────────────────────

export interface BearerAllowlistEntry {
  id: number
  provider_id: string
  host_pattern: string
  added_by: string
  added_at: string
}

export const useBearerAllowlist = () =>
  useQuery({
    queryKey: ['oauth-bearer-allowlist'],
    queryFn: async (): Promise<BearerAllowlistEntry[]> => {
      const res = await apiFetch('/v1/admin/oauth-bearer-allowlist')
      if (!res.ok) throw new Error('Failed to load allowlist')
      const data = await res.json()
      return data.entries || []
    },
  })

export const useAddBearerAllowlist = () => {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: async ({
      providerId,
      hostPattern,
    }: {
      providerId: string
      hostPattern: string
    }) => {
      const res = await apiFetch('/v1/admin/oauth-bearer-allowlist', {
        method: 'POST',
        body: JSON.stringify({
          provider_id: providerId,
          host_pattern: hostPattern,
        }),
      })
      if (!res.ok) {
        const err = await res.json().catch(() => ({}))
        throw new Error(err.detail || 'Failed to add')
      }
      return res.json()
    },
    onSuccess: () =>
      qc.invalidateQueries({ queryKey: ['oauth-bearer-allowlist'] }),
  })
}

export const useRemoveBearerAllowlist = () => {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: async (rowId: number) => {
      const res = await apiFetch(
        `/v1/admin/oauth-bearer-allowlist/${rowId}`,
        { method: 'DELETE' },
      )
      if (!res.ok) throw new Error('Failed to remove')
      return res.json()
    },
    onSuccess: () =>
      qc.invalidateQueries({ queryKey: ['oauth-bearer-allowlist'] }),
  })
}

export const useRestoreBearerAllowlist = () => {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: async () => {
      const res = await apiFetch(
        '/v1/admin/oauth-bearer-allowlist/restore-defaults',
        { method: 'POST' },
      )
      if (!res.ok) throw new Error('Failed to restore defaults')
      return res.json()
    },
    onSuccess: () =>
      qc.invalidateQueries({ queryKey: ['oauth-bearer-allowlist'] }),
  })
}
