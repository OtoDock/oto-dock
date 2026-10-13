import { Outlet, useLocation } from 'react-router-dom'
import ErrorBoundary from './ErrorBoundary'

/**
 * A crash in one page shows that page's card and leaves the rest of the app
 * running; leaving the page clears it. Used as a layout route around the
 * pages, and inside the admin and agent-settings layouts so their sidebars
 * stay. The root boundary in main.tsx still catches what happens outside
 * the routes.
 */
export function PageBoundary({ children }: { children: React.ReactNode }) {
  const { pathname } = useLocation()
  return <ErrorBoundary variant="route" resetKey={pathname}>{children}</ErrorBoundary>
}

export default function RouteBoundary() {
  return <PageBoundary><Outlet /></PageBoundary>
}
