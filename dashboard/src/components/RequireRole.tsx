import { Outlet, Navigate } from 'react-router-dom'
import { useAuth } from '../contexts/AuthContext'
import { PLATFORM_RANK, ROLE, type PlatformRole } from '../lib/permissions'

interface RequireRoleProps {
  minRole: Exclude<PlatformRole, 'member'>
}

/** The platform-role gate for a route: a role the table does not know is
 * redirected, and a floor it does not know admits nobody but the admin. */
export default function RequireRole({ minRole }: RequireRoleProps) {
  const { user } = useAuth()
  if (!user) return <Navigate to="/" replace />

  const userRank = PLATFORM_RANK[user.role] ?? -1
  const requiredRank = PLATFORM_RANK[minRole] ?? PLATFORM_RANK[ROLE.ADMIN]

  if (userRank < requiredRank) {
    return <Navigate to="/" replace />
  }

  return <Outlet />
}
