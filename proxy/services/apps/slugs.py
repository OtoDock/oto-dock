"""An app's slug: one pattern for the manifest, the deploy paths and the
agent template loader. A standard-library leaf, so the template loader
pulls in nothing of the apps platform."""

import re

#: 1 to 40 characters of ``[a-z0-9-]``, starting with a letter or a digit.
#: Match it with ``fullmatch``: with ``$``, ``match`` lets a trailing
#: newline through.
APP_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,39}$")
