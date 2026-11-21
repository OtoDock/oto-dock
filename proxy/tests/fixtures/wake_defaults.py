"""What a wake names for the rung that starts a session: the mode, the kind
and the builder every delegate result, stored wake and continuation passes
(``services/scheduler/delivery.py``). The rung has no default of its own."""
from core.permission_modes import UNATTENDED_MODE
from core.session import chat_rebuild

WAKE = {"permission_mode": UNATTENDED_MODE, "client_type": "", "build": chat_rebuild.AS_WAKE}
