"""Deprecated: ``Assistant`` moved to :mod:`kokua.core`.

Kept for one release because this is the import a front-end plugin would have copied from
``kokua.frontends.cli``. New code should use ``from kokua.core import Assistant``.
"""

import warnings

from kokua.core import Assistant, ModelClientError, ModelConnectionError

warnings.warn(
    "kokua.assistant has moved to kokua.core; this alias will be removed in a future release.",
    DeprecationWarning,
    stacklevel=2,
)

__all__ = ["Assistant", "ModelClientError", "ModelConnectionError"]
