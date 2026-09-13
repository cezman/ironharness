"""Unit tests of the shared async stdin pump (IH-39): the overflow cap and
the broken-pipe latch, against a real os.pipe nobody drains.
"""

from __future__ import annotations

import os

from ironbench.runner_common import _StdinWriter


def test_pump_overflow_latches_at_enqueue_and_broken_latches_on_pipe_death():
    rfd, wfd = os.pipe()
    writer = _StdinWriter(os.fdopen(wfd, "wb"))
    try:
        chunk = b"X" * (1 << 19)  # 512 KB: the pump blocks inside the first
        # chunk's write (the pipe buffer is far smaller), one chunk sits in
        # the queue, and the fourth enqueue crosses the 1 MiB cap
        for _ in range(4):
            writer.write(chunk)
        assert writer.overflow(), "the 2 MiB backlog did not latch the overflow cap"
        writer.close()
        # the pump is stuck inside a blocking write the reader never drains:
        # close() alone must not let join() lie about the thread being alive
        assert not writer.join(timeout=1), (
            "the pump exited while its blocking write should still be stuck"
        )
        os.close(rfd)  # kill the pipe: the stuck write must fail with OSError
        assert writer.join(timeout=5), "the pump did not exit after its pipe died"
        assert writer.broken(), "a failed pump write did not latch the broken flag"
    finally:
        try:
            os.close(rfd)
        except OSError:
            pass
