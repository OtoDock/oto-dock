/** The sentence a machine's card shows while install ahead installs tools. */

/** The most tools a card names; past it the rest are counted. */
const NAMED = 3

function listed(names: string[]): string {
  if (names.length > NAMED) return `${names.slice(0, NAMED).join(', ')} and ${names.length - NAMED} more`
  return names.length === 1 ? names[0] : `${names.slice(0, -1).join(', ')} and ${names[names.length - 1]}`
}

export function machineToolsSentence(installing: string[]): string {
  return installing.length === 1
    ? `Installing ${installing[0]}.`
    : `Installing ${installing.length} tools: ${listed(installing)}.`
}
