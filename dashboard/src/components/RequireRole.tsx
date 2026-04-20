import { Outlet, Navigate } from 'react-router-dom'
import { useAuth } from '../contexts/AuthContext'

const ROLE_LEVEL: Record<string, number> = {
  admin: 0,
  creator: 1,
  member: 2,
}

interface RequireRoleProps {
  minRole: 'admin' | 'creator'
}

export default function RequireRole({ minRole }: RequireRoleProps) {
  const { user } = useAuth()
  if (!user) return <Navigate to="/" replace />

  const userLevel = ROLE_LEVEL[user.role] ?? 99
  const requiredLevel = ROLE_LEVEL[minRole] ?? 0

  if (userLevel > requiredLevel) {
    return <Navigate to="/" replace />
  }

  return <Outlet />
}
