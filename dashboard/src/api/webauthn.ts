// WebAuthn passkey API — registration + login ceremonies.
//
// The proxy speaks the WebAuthn JSON wire format (base64url strings); the
// browser credential API wants ArrayBuffers. The helpers below convert both
// ways manually — PublicKeyCredential.parseCreationOptionsFromJSON() would do
// it natively but isn't available on all supported browsers yet.

import type { User } from './auth'
import { apiFetch } from './auth'

export interface PasskeyInfo {
  credential_id: string
  name: string
  created_at: string
  last_used: string | null
}

export function passkeySupported(): boolean {
  return typeof window !== 'undefined' && !!window.PublicKeyCredential
}

function b64urlToBuf(s: string): ArrayBuffer {
  const pad = '='.repeat((4 - (s.length % 4)) % 4)
  const bin = atob(s.replace(/-/g, '+').replace(/_/g, '/') + pad)
  const buf = new Uint8Array(bin.length)
  for (let i = 0; i < bin.length; i++) buf[i] = bin.charCodeAt(i)
  return buf.buffer
}

function bufToB64url(buf: ArrayBuffer): string {
  const bytes = new Uint8Array(buf)
  let bin = ''
  for (const b of bytes) bin += String.fromCharCode(b)
  return btoa(bin).replace(/\+/g, '-').replace(/\//g, '_').replace(/=+$/, '')
}

async function jsonOrThrow(res: Response, fallback: string) {
  if (!res.ok) {
    const detail = await res.json().catch(() => ({}))
    throw new Error(detail.detail || fallback)
  }
  return res.json()
}

// --- Registration (authed, password-confirmed) ---

export async function listPasskeys(): Promise<{ passkeys: PasskeyInfo[]; enabled: boolean }> {
  const res = await apiFetch('/v1/users/me/passkeys')
  return jsonOrThrow(res, 'Failed to load passkeys')
}

export async function registerPasskey(password: string, name: string): Promise<void> {
  const optRes = await apiFetch('/v1/users/me/passkeys/register/options', {
    method: 'POST',
    body: JSON.stringify({ password }),
  })
  const { state, options } = await jsonOrThrow(optRes, 'Failed to start passkey registration')

  const publicKey: PublicKeyCredentialCreationOptions = {
    ...options,
    challenge: b64urlToBuf(options.challenge),
    user: { ...options.user, id: b64urlToBuf(options.user.id) },
    excludeCredentials: (options.excludeCredentials || []).map((c: any) => ({
      ...c, id: b64urlToBuf(c.id),
    })),
  }
  const cred = (await navigator.credentials.create({ publicKey })) as PublicKeyCredential | null
  if (!cred) throw new Error('Passkey creation was cancelled')
  const attResponse = cred.response as AuthenticatorAttestationResponse

  const verifyRes = await apiFetch('/v1/users/me/passkeys/register/verify', {
    method: 'POST',
    body: JSON.stringify({
      state,
      name,
      credential: {
        id: cred.id,
        rawId: bufToB64url(cred.rawId),
        type: cred.type,
        response: {
          clientDataJSON: bufToB64url(attResponse.clientDataJSON),
          attestationObject: bufToB64url(attResponse.attestationObject),
          transports: attResponse.getTransports?.() ?? [],
        },
        clientExtensionResults: cred.getClientExtensionResults(),
      },
    }),
  })
  await jsonOrThrow(verifyRes, 'Passkey registration failed')
}

export async function renamePasskey(credentialId: string, name: string, password: string): Promise<void> {
  const res = await apiFetch(`/v1/users/me/passkeys/${encodeURIComponent(credentialId)}`, {
    method: 'PUT',
    body: JSON.stringify({ name, password }),
  })
  await jsonOrThrow(res, 'Failed to rename passkey')
}

export async function deletePasskey(credentialId: string, password: string): Promise<void> {
  const res = await apiFetch(`/v1/users/me/passkeys/${encodeURIComponent(credentialId)}`, {
    method: 'DELETE',
    body: JSON.stringify({ password }),
  })
  await jsonOrThrow(res, 'Failed to remove passkey')
}

// --- Login (public) ---

// The state marker the native-handoff deep link carries in place of an OIDC
// state (otodock://auth/callback?code=<token>&state=passkey-handoff) — must
// match the proxy's NATIVE_HANDOFF_STATE.
export const NATIVE_HANDOFF_STATE = 'passkey-handoff'

async function runPasskeyAssertion(native: boolean, totpSessionToken?: string): Promise<any> {
  const optRes = await fetch('/auth/passkey/options', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(totpSessionToken ? { totp_session_token: totpSessionToken } : {}),
  })
  const { state, options } = await jsonOrThrow(optRes, 'Failed to start passkey sign-in')

  const publicKey: PublicKeyCredentialRequestOptions = {
    ...options,
    challenge: b64urlToBuf(options.challenge),
    allowCredentials: (options.allowCredentials || []).map((c: any) => ({
      ...c, id: b64urlToBuf(c.id),
    })),
  }
  const cred = (await navigator.credentials.get({ publicKey })) as PublicKeyCredential | null
  if (!cred) throw new Error('Passkey sign-in was cancelled')
  const assertion = cred.response as AuthenticatorAssertionResponse

  const verifyRes = await fetch('/auth/passkey/verify', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    credentials: 'same-origin',
    body: JSON.stringify({
      state,
      native,
      ...(totpSessionToken ? { totp_session_token: totpSessionToken } : {}),
      credential: {
        id: cred.id,
        rawId: bufToB64url(cred.rawId),
        type: cred.type,
        response: {
          clientDataJSON: bufToB64url(assertion.clientDataJSON),
          authenticatorData: bufToB64url(assertion.authenticatorData),
          signature: bufToB64url(assertion.signature),
          userHandle: assertion.userHandle ? bufToB64url(assertion.userHandle) : null,
        },
        clientExtensionResults: cred.getClientExtensionResults(),
      },
    }),
  })
  return jsonOrThrow(verifyRes, 'Passkey sign-in failed')
}

export async function passkeyLogin(): Promise<User> {
  const data = await runPasskeyAssertion(false)
  return data.user
}

/** 2FA-step leg: complete the second factor with a passkey (consumes the step token). */
export async function passkeySecondFactor(totpSessionToken: string): Promise<User> {
  const data = await runPasskeyAssertion(false, totpSessionToken)
  return data.user
}

/** System-browser leg of the native-app flow: returns the one-time handoff token. */
export async function nativePasskeyLogin(): Promise<string> {
  const data = await runPasskeyAssertion(true)
  return data.native_token
}

/** App-webview leg: trade the handoff token for this webview's session cookie. */
export async function exchangeNativeToken(token: string): Promise<User> {
  const res = await fetch('/auth/passkey/native/exchange', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    credentials: 'same-origin',
    body: JSON.stringify({ token }),
  })
  const data = await jsonOrThrow(res, 'Passkey sign-in failed')
  return data.user
}
