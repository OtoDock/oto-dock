// Version-annotated CLI chips for the machine cards: append the version the
// satellite's pin reconcile actually resolved, and flag drift from the
// platform's pinned version. Pure so the mismatch logic is unit-testable.

export interface CliChipInfo {
  label: string
  mismatch: boolean
  title?: string
}

export function cliChipInfo(
  cliName: string,
  cliStatus?: Record<string, { version?: string | null; path?: string | null }>,
  pins?: Record<string, string>,
  // capabilities.installed_clis names → cli_status/cli_pins keys (bin names),
  // from the engine descriptors (lib/engines installedCliBinaries).
  binaries: Record<string, string> = {},
): CliChipInfo {
  const key = binaries[cliName]
  const version = (key && cliStatus?.[key]?.version) || null
  if (!version) {
    // Old satellite / pre-reconcile window / unknown CLI name — plain chip.
    return { label: cliName, mismatch: false }
  }
  const pin = (key && pins?.[key]) || ''
  const mismatch = !!pin && version !== pin
  return {
    label: `${cliName} ${version}`,
    mismatch,
    title: mismatch ? `Platform pin: ${pin}` : undefined,
  }
}
