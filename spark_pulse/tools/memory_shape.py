"""What shape a node's free memory is in, from ``/proc/buddyinfo``.

``MemAvailable`` says how much memory is free. It does not say whether that
memory comes in pieces a GPU can use: on a unified-memory machine (a GB10) the
driver maps the model's weights and KV cache with large pages, and a large page
needs a physically contiguous block under it. A node nine days up can report
18 GiB free with nothing larger than 256 MiB in one piece, and an engine that
starts there gets its weights scattered over small blocks.

The buddy allocator already keeps the answer: for each *order* ``n`` it holds
some number of free blocks of ``page_size << n`` bytes. The agent reports those
counts raw (``BuddyInfo``, summed over every zone), and this module is where
they become a reading — pure, so the node page and the pre-flight say the same
thing about the same numbers.

The threshold is :data:`HUGE_BLOCK_BYTES`, 2 MiB: the smallest block a GPU
large-page mapping is made of, and the figure the "free in blocks of 2 MiB or
more" in the post this is drawn from (and in our own measurements) refers to.
Free memory in this sense is the buddy allocator's free memory only — it
excludes the page cache, which ``MemAvailable`` counts as reclaimable. So
``free_bytes`` here is usually well below ``MemAvailable``, and that gap is
itself the reading: what dropping the caches would add.
"""

from __future__ import annotations

from typing import Any, Iterable

#: The smallest block a GPU large-page mapping is built from.
HUGE_BLOCK_BYTES = 2 * 1024 * 1024

#: Below this share of free memory in blocks of 2 MiB or more, the free memory
#: is shredded: compaction would help, and an engine started now gets small
#: blocks. A first threshold, to be moved by measurement, not by argument.
FRAGMENTED_BELOW_PERCENT = 50.0

#: Too little free memory to say anything about its shape. A node whose page
#: cache holds nearly everything has a few hundred MiB free in whatever shape,
#: and calling that fragmented would be a warning about nothing.
MIN_FREE_BYTES = 1024 * 1024 * 1024


def parse_buddyinfo(text: str) -> list[int]:
    """Free blocks per order, summed over zones — the agent's rule, in Python.

    The pre-flight reads ``/proc/buddyinfo`` through the host probe, as it
    reads ``/proc/meminfo``; this is the same sum ``agent/src/executor/stats.rs``
    makes, so both paths hand :func:`summarize` the same list.
    """
    sums: list[int] = []
    for line in text.splitlines():
        _, sep, rest = line.partition("zone")
        if not sep:
            continue
        fields = rest.split()[1:]
        try:
            counts = [int(field) for field in fields]
        except ValueError:
            continue
        if len(sums) < len(counts):
            sums.extend([0] * (len(counts) - len(sums)))
        for order, count in enumerate(counts):
            sums[order] += count
    return sums


def summarize(page_size: int, free_blocks: Iterable[int]) -> dict[str, Any] | None:
    """The reading: free bytes, how much of it is contiguous, the largest block.

    ``None`` when there is nothing to read (no counts, or no page size), which
    the caller renders as "not reported" rather than as an empty machine.
    """
    counts = [int(c) for c in free_blocks]
    if page_size <= 0 or not counts:
        return None

    free = 0
    contiguous = 0
    largest = 0
    blocks = []
    for order, count in enumerate(counts):
        size = page_size << order
        total = size * count
        free += total
        if size >= HUGE_BLOCK_BYTES:
            contiguous += total
        if count:
            largest = size
        blocks.append({"block_bytes": size, "count": count, "bytes": total})

    percent = round(contiguous / free * 100, 1) if free else 0.0
    return {
        "page_size_bytes": page_size,
        "free_bytes": free,
        "contiguous_bytes": contiguous,
        "contiguous_percent": percent,
        "largest_block_bytes": largest,
        "fragmented": free >= MIN_FREE_BYTES and percent < FRAGMENTED_BELOW_PERCENT,
        "blocks": blocks,
    }


__all__ = [
    "FRAGMENTED_BELOW_PERCENT",
    "HUGE_BLOCK_BYTES",
    "MIN_FREE_BYTES",
    "parse_buddyinfo",
    "summarize",
]
