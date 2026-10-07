// The reverse-proxy misconfigurations the proxy reports (auth/lan_check.py
// forwarding_warnings), worded for the admin: the Security tab lists every
// row, the admin banner shows the first. TRUSTED_PROXY is a list, so an
// address is added to it, never set over it. The container's gateway may be
// trusted only while the port is published on 127.0.0.1: direct connections
// to a published port arrive from it too, so trusting it otherwise lets any
// such client forge its address. On the OtoDock cloud the operator owns the
// configuration, so the admin gets no instruction.

export interface ForwardingWarning {
  peer: string
  // True when the peer is the container's gateway (a published port's clients arrive from it too)
  gateway?: boolean
  // 'no_trusted_proxy': the boot check, an https public URL with no TRUSTED_PROXY in a container
  case: 'untrusted_forwarder' | 'edge_without_xff' | 'no_trusted_proxy' | string
  first_seen: number | string   // epoch seconds
  last_seen: number | string
  count: number
}

const LIST = '(a comma-separated list: that address, never a subnet)'

function untrustedForwarderAdvice(w: ForwardingWarning, cloud: boolean): string {
  const head = `Forwarding headers arrive from ${w.peer}`
  if (cloud) return `${head}, which is not a trusted proxy.`
  if (w.gateway) {
    return `${head}, the container's gateway, which is not a trusted proxy. `
      + 'Trust it only with PROXY_BIND_IP=127.0.0.1 (the port published on this '
      + "host's loopback only): direct connections to a published port arrive from "
      + `the gateway too. Then add ${w.peer} to TRUSTED_PROXY in .env ${LIST}.`
  }
  return `${head}, which is not a trusted proxy: if it is your reverse proxy, add `
    + `${w.peer} to TRUSTED_PROXY in config.env, or .env on a Docker install ${LIST}.`
}

function noTrustedProxyAdvice(w: ForwardingWarning, cloud: boolean): string {
  if (cloud) return 'No reverse proxy is trusted, so everyone behind it shares its address.'
  // The proxy raises this row when no trusted hop is in effect: TRUSTED_PROXY
  // empty, or every entry ignored (lan_check._unconfigured_edge), such as a
  // loopback entry in a container, which never matches the edge.
  const head = 'The public URL is https but no reverse proxy is trusted (TRUSTED_PROXY is '
    + 'empty, or every entry in it is ignored, such as a loopback address in a container), '
    + 'so everyone behind your reverse proxy shares its address'
  const gateway = w.peer ? `the container's gateway ${w.peer}` : "the container's gateway"
  return `${head}. If the reverse proxy runs on this host, publish the port on `
    + `127.0.0.1 only (PROXY_BIND_IP=127.0.0.1) and add ${gateway} to TRUSTED_PROXY `
    + "in .env; if it runs on another machine, add that machine's address "
    + `${LIST}.`
}

export function forwardingAdvice(w: ForwardingWarning, cloud: boolean): string {
  if (w.case === 'edge_without_xff') return `The trusted proxy ${w.peer} does not append X-Forwarded-For.`
  if (w.case === 'no_trusted_proxy') return noTrustedProxyAdvice(w, cloud)
  return untrustedForwarderAdvice(w, cloud)
}

// The proxy stamps a warning with epoch seconds; an ISO string is accepted too.
export function lastSeen(v: number | string): string {
  const d = typeof v === 'number' ? new Date(v * 1000) : new Date(v)
  return Number.isNaN(d.getTime()) ? String(v) : d.toLocaleString()
}

// "(3 requests, last …)" for a row the detector counted; the boot check
// counts no requests.
export function forwardingSeen(w: ForwardingWarning): string {
  if (!w.count) return ''
  return `(${w.count} request${w.count === 1 ? '' : 's'}, last ${lastSeen(w.last_seen)})`
}

export const FORWARDING_CONSEQUENCE = 'Until the address is trusted, everyone behind it '
  + 'shares one sign-in bucket and local-network-only accounts cannot sign in through it.'
