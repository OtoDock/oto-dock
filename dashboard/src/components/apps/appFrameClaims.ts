// The viewer claims in hand, per app id and instance, shared by every frame
// instance (AppFrame): a remount (a keyed tab switch and back) reuses the
// claim instead of minting again inside the route's 2 s pace, and a claim is
// only ever posted into the frame of the app it was minted for (a claim
// carries the viewer's identity and role, so one app's page must never
// receive another's). The claim also names the server the page's calls
// reach: the working copy's page must never hold the live release's claim,
// nor the live page the preview's.

export interface ViewerClaim {
  token: string
  exp: number
  at: number
}

const viewerClaims = new Map<string, ViewerClaim>()

export const claimKey = (appId: string, preview: boolean) => `${appId}|${preview ? 'preview' : 'live'}`

export function heldClaim(key: string): ViewerClaim | undefined {
  return viewerClaims.get(key)
}

export function holdClaim(key: string, claim: ViewerClaim): void {
  viewerClaims.set(key, claim)
}

export function _resetViewerClaimsForTests(): void { viewerClaims.clear() }
