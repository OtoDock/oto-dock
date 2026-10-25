import { useAuth } from '../../contexts/AuthContext'
import { roleBadge, roleLabel } from '../../lib/permissions'
import Button from '../ui/Button'
import { BADGE_BASE } from '../ui/classes'
import { IconArrowLeft } from '../icons/nav'

/** The signed-in person and their role, with Logout, at the foot of a
 *  settings area's sidebar and of its phone list. */
export function AccountFooter() {
  const { user, logout } = useAuth()
  if (!user) return null
  return (
    <div className="p-3 border-t border-subtle">
      <p className="text-xs text-secondary truncate">{user.name}</p>
      <div className="flex items-center gap-2 mt-1">
        <span className={`${BADGE_BASE} ${roleBadge(user.role)}`}>
          {roleLabel(user.role)}
        </span>
        <button
          type="button"
          onClick={logout}
          className="text-xs text-tertiary hover:text-secondary transition-colors duration-fast"
        >
          Logout
        </button>
      </div>
    </div>
  )
}

/** The way out of a settings area: `useSettingsArea`'s `backToChat`. */
export function BackToChatButton({ onClick }: { onClick: () => void }) {
  return (
    <Button variant="primary" size="sm" className="w-full" onClick={onClick}>
      <IconArrowLeft className="w-3.5 h-3.5" />
      Back to Chat
    </Button>
  )
}
