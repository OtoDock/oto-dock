"""What a WebSocket raises once its peer is gone.

A socket's own calls (``accept``, a read, a send) raise one of several
exceptions when the other side has left, depending on the server and on the
moment: Starlette's disconnect frame, the websockets library's close,
uvicorn's lost connection, and, while the socket opens or after uvicorn
skipped the handshake, a state ``RuntimeError``. These are a disconnect, not
a fault: the handler ends quietly and its cleanup runs.
"""

from fastapi import WebSocketDisconnect
from uvicorn.protocols.utils import ClientDisconnected
from websockets.exceptions import ConnectionClosed

# The peer is gone: a disconnect frame, the websockets library's close,
# uvicorn's lost connection.
GONE = (WebSocketDisconnect, ConnectionClosed, ClientDisconnected)
# While the socket opens, also the state error a call meets on a socket that
# closed under it. Caught around the socket's own calls only: anywhere else a
# RuntimeError is a fault and must surface.
GONE_WHILE_OPENING = (*GONE, RuntimeError)

# The messages of the state errors a closed socket raises: Starlette's ("not
# connected", "accept", "disconnect message") and uvloop's closed transport.
_CLOSED_SOCKET_WORDS = ("not connected", "accept", "disconnect message",
                        "the handler is closed", "closed=True")


def closed_socket_error(e: BaseException) -> bool:
    """A ``RuntimeError`` a call on an already closed socket raises, told
    from any other by its message."""
    return isinstance(e, RuntimeError) and any(w in str(e) for w in _CLOSED_SOCKET_WORDS)
