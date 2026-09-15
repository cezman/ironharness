"""Incremental UTF-8 decoding for serial chunk loops (IH-61).

One implementation of the hold-partial-tail decode behavior instead of one
per loop: a multibyte character split by a TCP/serial chunk boundary used to
become two U+FFFD replacements in every consumer that decoded per chunk
(renode telnet filter and the real pump - IH-58). feed() never loses a
partial character: it is held until the completing bytes arrive.
"""

from __future__ import annotations

import codecs


class Utf8StreamDecoder:
    """Wraps codecs.getincrementaldecoder("utf-8"): decode(raw bytes) returns
    the decoded text for everything received SO FAR - a trailing partial
    multibyte character is held internally and completed by the next call."""

    def __init__(self, errors: str = "replace") -> None:
        self._dec = codecs.getincrementaldecoder("utf-8")(errors)

    def decode(self, data: bytes) -> str:
        return self._dec.decode(data)
