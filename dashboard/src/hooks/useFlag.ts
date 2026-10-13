import { useAuth } from '../contexts/AuthContext'
import type { User } from '../api/auth'

type Flags = NonNullable<User['feature_flags']>
export type FlagName = keyof Flags
type FlagValue<K extends FlagName> = NonNullable<Flags[K]>

/** What a flag reads as when /auth/me does not carry it (an older proxy, a
 *  user snapshot not loaded yet). The switches fail OPEN: a missing flag
 *  never hides a feature the server did not turn off; the size caps fall
 *  back to the shipped defaults of OTODOCK_MAX_FILE_MB and
 *  RECOVER_BIN_MAX_MB. */
export const FLAG_DEFAULTS: { [K in FlagName]-?: FlagValue<K> } = {
  allow_user_paired_machines: true,
  remote_machines_available: true,
  interactive_terminal_enabled: true,
  upload_max_bytes: 1024 * 1024 * 1024,
  recover_bin_max_bytes: 100 * 1024 * 1024,
}

export function flagValue<K extends FlagName>(user: User | null | undefined, name: K): FlagValue<K> {
  return (user?.feature_flags?.[name] ?? FLAG_DEFAULTS[name]) as FlagValue<K>
}

/** One feature flag of the signed-in user, or its default. */
export function useFlag<K extends FlagName>(name: K): FlagValue<K> {
  const { user } = useAuth()
  return flagValue(user, name)
}
