"""The transport-agnostic assistant runtime.

``Assistant`` is the composition root and the serve loop; it delegates conversations to
``ConversationBook``, turn execution to ``TurnRunner``, human decisions to ``HumanGate``, and
settings to ``SettingsApplier``. Nothing here knows what a terminal or a socket is: the only view of
the outside world is a ``ChannelUI``.
"""

from .assistant import Assistant, ModelClientError, ModelConnectionError

__all__ = ["Assistant", "ModelClientError", "ModelConnectionError"]
