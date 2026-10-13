// The platform version the server reports on the dashboard socket
// (`server_info.version`, read by useDashboardWs): the OtoDock tab's "This
// install" card falls back to it when the REST answer carries none (a dev
// tree without VERSIONS.md).

let version = ''

export function noteServerVersion(value: unknown): void {
  if (typeof value === 'string') version = value
}

export function serverVersion(): string {
  return version
}
