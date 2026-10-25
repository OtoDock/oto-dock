import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { apiFetch } from './auth'
import { apiJson, withMessage, apiOk } from '../lib/http/apiJson'
import { eventCatalogKey } from './subscriptions'

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
      return apiJson<OAuthAccountsResponse>(
        `/v1/oauth/${provider}/accounts?mcp_name=${encodeURIComponent(mcpName)}`,
      ).catch(withMessage('Failed to load OAuth accounts'))
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
  /** The kind of app the person picked (`oauth_meta.connect_kinds`); empty
   * takes the kind in use. */
  appKind?: string
}

/**
 * The app's half of a connect it started: the otodock:// link brings the
 * vendor's code and the state back, and the exchange runs here under the
 * app's own login (the server exchanges nothing on the callback for an app
 * connect). A link without a code is an older server's, already exchanged.
 */
export async function exchangeOAuthCode(provider: string, link: string): Promise<void> {
  let params: URLSearchParams
  try {
    params = new URL(link).searchParams
  } catch {
    return
  }
  const code = params.get('code')
  const state = params.get('state')
  if (!code || !state) return
  await apiOk(`/v1/oauth/${provider}/exchange`, {
    method: 'POST',
    body: JSON.stringify({ code, state, iss: params.get('iss') || '' }),
  }, 'Failed to complete the connect')
}

export const useStartOAuth = () => {
  return useMutation({
    mutationFn: async (params: OAuthStartParams): Promise<{ url: string }> => {
      return apiJson<{ url: string }>(`/v1/oauth/${params.provider}/start`, {
        method: 'POST',
        body: JSON.stringify({
          mcp_name: params.mcpName,
          services: params.services,
          account_label: params.accountLabel || '',
          is_service: params.isService || false,
          mobile: params.mobile || false,
          app_kind: params.appKind || '',
        }),
      }, 'Failed to start OAuth')
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
      return apiJson<DeviceCodeStartResult>(`/v1/oauth/${params.provider}/device-code/start`, {
        method: 'POST',
        body: JSON.stringify({
          mcp_name: params.mcpName,
          services: params.services,
          account_label: params.accountLabel || '',
          is_service: params.isService || false,
        }),
      }, 'Failed to start device-code flow')
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
      return apiJson<{ status: string; email: string; account_label: string }>(`/v1/oauth/${params.provider}/pat/save`, {
        method: 'POST',
        body: JSON.stringify({
          mcp_name: params.mcpName,
          token: params.token,
          services: params.services,
          account_label: params.accountLabel || '',
          is_service: params.isService || false,
        }),
      }, 'Failed to save token')
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
      return apiJson(`/v1/oauth/${provider}/disconnect`, {
        method: 'POST',
        body: JSON.stringify({
          mcp_name: mcpName,
          account_label: accountLabel,
          is_service: isService,
        }),
      }, 'Failed to disconnect')
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
// GitHub App installations
// ───────────────────────────────────────────────────────────────────

/** GitHub's page to install the app for one of the caller's GitHub-App
 * accounts; the page returns to OtoDock, which re-lists the installations. */
export const useAppInstallUrl = () => {
  return useMutation({
    mutationFn: async (params: {
      provider: string
      mcpName: string
      accountLabel: string
      mobile?: boolean
    }): Promise<{ url: string }> => {
      return apiJson<{ url: string }>(`/v1/oauth/${params.provider}/app/install-url`, {
        method: 'POST',
        body: JSON.stringify({
          mcp_name: params.mcpName,
          account_label: params.accountLabel,
          mobile: params.mobile || false,
        }),
      }, 'Failed to open the install page')
    },
  })
}

/** Re-list the installations of one of the caller's GitHub-App accounts. */
export const useRefreshInstallations = () => {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: async (params: {
      provider: string
      mcpName: string
      accountLabel: string
    }): Promise<{ status: string; installations: unknown[] }> => {
      return apiJson<{ status: string; installations: unknown[] }>(
        `/v1/oauth/${params.provider}/app/installations/refresh`, {
          method: 'POST',
          body: JSON.stringify({ mcp_name: params.mcpName, account_label: params.accountLabel }),
        }, 'Failed to refresh the installations')
    },
    onSuccess: (_data, params) => {
      qc.invalidateQueries({ queryKey: ['my-integrations'] })
      qc.invalidateQueries({ queryKey: eventCatalogKey(params.mcpName) })
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
      return apiJson<{ url: string }>('/v1/oauth/microsoft/admin-consent/start', {
          method: 'POST',
          body: JSON.stringify({
            mcp_name: params.mcpName,
            mobile: params.mobile || false,
          }),
        }, 'Failed to start admin-consent flow')
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
      const data = await apiJson<{ entries?: BearerAllowlistEntry[] }>('/v1/admin/oauth-bearer-allowlist')
        .catch(withMessage('Failed to load allowlist'))
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
      return apiJson('/v1/admin/oauth-bearer-allowlist', {
        method: 'POST',
        body: JSON.stringify({
          provider_id: providerId,
          host_pattern: hostPattern,
        }),
      }, 'Failed to add')
    },
    onSuccess: () =>
      qc.invalidateQueries({ queryKey: ['oauth-bearer-allowlist'] }),
  })
}

export const useRemoveBearerAllowlist = () => {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: async (rowId: number) => {
      return apiJson(
        `/v1/admin/oauth-bearer-allowlist/${rowId}`,
        { method: 'DELETE' },
      ).catch(withMessage('Failed to remove'))
    },
    onSuccess: () =>
      qc.invalidateQueries({ queryKey: ['oauth-bearer-allowlist'] }),
  })
}

export const useRestoreBearerAllowlist = () => {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: async () => {
      return apiJson(
        '/v1/admin/oauth-bearer-allowlist/restore-defaults',
        { method: 'POST' },
      ).catch(withMessage('Failed to restore defaults'))
    },
    onSuccess: () =>
      qc.invalidateQueries({ queryKey: ['oauth-bearer-allowlist'] }),
  })
}

// ───────────────────────────────────────────────────────────────────
// The OAuth clients the install registered at vendors (admin)
// ───────────────────────────────────────────────────────────────────

// One registration of this install at an authorization server, for one
// callback URL; a revoked row stays listed (token files point at it).
export interface ClientRegistration {
  id: number
  issuer: string
  redirect_uri: string
  registration_endpoint: string
  client_id: string
  client_secret_expires_at: string
  token_endpoint_auth_method: string
  registration_client_uri: string
  client_name: string
  scope: string
  created_at: string
  last_used_at: string
  revoked_at: string
  revoked_reason: string
  // The token endpoint auth method the install asked for, and the
  // space-separated MCP server URLs that signed in through this row.
  requested_auth_method: string
  resources: string
  has_secret: boolean
  mcps: string[]
}

export const useClientRegistrations = (enabled = true) =>
  useQuery({
    queryKey: ['oauth-client-registrations'],
    enabled,
    queryFn: async (): Promise<ClientRegistration[]> => {
      const data = await apiJson<{ registrations?: ClientRegistration[] }>('/v1/admin/oauth-client-registrations')
        .catch(withMessage('Failed to load registrations'))
      return data.registrations || []
    },
  })

export const useForgetClientRegistration = () => {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: async (rowId: number) => {
      return apiJson(`/v1/admin/oauth-client-registrations/${rowId}/forget`, { method: 'POST' }, 'Failed to forget the registration')
    },
    onSuccess: () =>
      qc.invalidateQueries({ queryKey: ['oauth-client-registrations'] }),
  })
}

