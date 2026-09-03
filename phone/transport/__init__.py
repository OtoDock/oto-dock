"""Media transports — the audio byte-plane a pipeline session runs on.

`MediaTransport` (base.py) is the seam between the conversation engine
(pipeline/) and whatever carries the audio: Asterisk AudioSocket today,
the dashboard duplex bridge and further telephony providers next.
"""

from .base import MediaTransport

__all__ = ["MediaTransport"]
