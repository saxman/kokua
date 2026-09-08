"""On-disk storage for oversized text payloads and the ``/payloads/<name>`` reference to one.

A sub-agent's tool result can be megabytes (a PDF fetched as text is the case that forced this), and
recording it inline in ``session.metadata`` put 51.9 MB of one developer's 56.8 MB session file
there. Every store read re-parsed all of it, every save rewrote all of it, and every switch replayed
all of it to the browser.

So the bytes live as files under ``config.payloads_path`` and the session holds a short reference
plus a preview. The web server serves the reference (see ``frontends/web.py``), and a reader who
wants the whole thing asks for it.

This is deliberately the same shape as ``images.py``, which solves the same problem for image bytes:
content-addressed filenames, a ``/<prefix>/<name>`` reference stored in place of the payload, and a
route that serves it. Two visibly parallel modules rather than one shared abstraction, because the
generic part is about twenty lines and the rest of each module is about its own payload kind.

Filenames are the sha256 of the text, so the same response recorded twice is stored once.
"""

from __future__ import annotations

import hashlib
import re
from pathlib import Path
from typing import Optional

# The public URL prefix the web server serves payloads at, and the marker identifying a reference
# inside stored session metadata. Kept together so both sides agree on one spelling.
ROUTE_PREFIX = "/payloads/"

# A payload's name is exactly a sha256 hex digest, which is what save_text writes and the only thing
# that can legitimately reach reference_to_path. Matched positively rather than merely screened for
# path separators: pathlib's own notion of "a bare basename" still lets ".." through (Path("..").name
# is "..", not ""), so a blocklist answers only the inputs someone thought to try. This allowlist
# instead accepts nothing that a real payload name would not already be.
_DIGEST_RE = re.compile(r"^[0-9a-f]{64}$")


def is_reference(value: str) -> bool:
    """True if *value* is one of our ``/payloads/<name>`` references."""
    return isinstance(value, str) and value.startswith(ROUTE_PREFIX) and len(value) > len(ROUTE_PREFIX)


def save_text(payloads_path: Path, text: str) -> str:
    """Write *text* under ``payloads_path`` and return its ``/payloads/<name>`` reference.

    The filename is the sha256 of the UTF-8 bytes, so saving identical text is idempotent and two
    conversations that fetched the same document share one stored copy.
    """
    data = text.encode("utf-8")
    name = hashlib.sha256(data).hexdigest()
    payloads_path.mkdir(parents=True, exist_ok=True)
    path = payloads_path / name
    if not path.exists():
        path.write_bytes(data)
    return f"{ROUTE_PREFIX}{name}"


def reference_to_path(payloads_path: Path, reference: str) -> Optional[Path]:
    """The file a ``/payloads/<name>`` reference names, or None if it names none.

    Returns None for anything whose name is not exactly a sha256 hex digest, rather than merely
    rejecting path separators: a name is only ever legitimate if ``save_text`` could have produced
    it, so requiring that shape rules out traversal (including ``..``, which ``Path("..").name``
    reports as ``".."`` rather than empty, so a separator-only check misses it) along with every
    other malformed name in one place, for every caller.
    """
    if not is_reference(reference):
        return None
    name = reference[len(ROUTE_PREFIX) :]
    if not _DIGEST_RE.match(name):
        return None
    return payloads_path / name


def read_text(payloads_path: Path, reference: str) -> Optional[str]:
    """The stored text a reference names, or None if the reference is bad or the file is gone.

    A missing file is not an error worth raising: payloads are not garbage collected, but a user who
    cleared the folder by hand should get a card without its full text rather than a broken page.
    """
    path = reference_to_path(payloads_path, reference)
    if path is None or not path.is_file():
        return None
    return path.read_text(encoding="utf-8")
