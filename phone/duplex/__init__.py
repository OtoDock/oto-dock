"""Dashboard full-duplex sessions — the conversation engine off telephony.

A duplex session is the phone pipeline (VAD → STT → turn classifier → LLM →
TTS, barge-in and all) running over the proxy's ``/ws/duplex-engine/{id}``
dial-back socket instead of an AudioSocket TCP connection: 16 kHz mic in,
24 kHz TTS out, the LLM turns attached to a normal dashboard chat by the
proxy. The daemon advertises the capability on the management socket and
receives ``duplex_open`` requests there.
"""

from .manager import DuplexManager, capabilities_frame, duplex_supported

__all__ = ["DuplexManager", "capabilities_frame", "duplex_supported"]
