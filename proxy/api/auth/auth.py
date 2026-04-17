"""Core authentication / user-management / platform-settings API — assembly.

The endpoints live in sibling modules — ``identity`` (login/session/self-
service), ``admin_users`` (admin user management), ``platform`` (settings/
license/SMTP) — and all attach to the shared ``api.auth._router.router``,
imported here so ``app.py`` can mount ``auth.router`` with every route.

Routes are grouped by section (order is unobservable: no two app routes
share a path+method)."""

# Section route modules — imported for their ``@router`` registrations (this is
# what attaches every endpoint to the shared router).
from api.auth import _router
from api.auth import identity as _identity
from api.auth import admin_users as _admin_users
from api.auth import platform as _platform
from api.auth import webauthn as _webauthn

# The shared router app.py mounts as ``auth.router`` (all section routes attached).
router = _router.router
# Re-export for the test-suite (``from api.auth.auth import _enforce_user_paired_disabled``).
_enforce_user_paired_disabled = _platform._enforce_user_paired_disabled
# Importing the section modules above is what registers their routes; the tuple
# keeps linters from flagging the imports as unused.
_SECTION_MODULES = (_identity, _admin_users, _platform, _webauthn)
