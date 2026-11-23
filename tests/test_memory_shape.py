"""The shape of a node's free memory, read from ``/proc/buddyinfo``.

``MemAvailable`` says how much is free; on unified memory the GPU maps its
weights with large pages, and a large page needs a contiguous block under it.
These hold the reading to what the kernel actually reports.
"""

from __future__ import annotations

from spark_pulse.tools import memory_shape

MIB = 1024 * 1024

#: What 192.168.29.60 printed on 2026-11-23: 4 KiB pages, 14 orders.
SPARK = (
    "Node 0, zone      DMA     36     34     44     51     72     64     62"
    "     34     25      5      4      7      5     10\n"
    "Node 0, zone   Normal   2195   1363   1804  14409    580    259    134"
    "     65     32     30      6      3      1      6\n"
)


def test_counts_are_summed_per_order_over_every_zone():
    assert memory_shape.parse_buddyinfo(SPARK)[:4] == [2231, 1397, 1848, 14460]
    assert len(memory_shape.parse_buddyinfo(SPARK)) == 14


def test_a_line_that_does_not_parse_adds_nothing():
    text = "garbage\nNode 0, zone Normal 1 x 3\nNode 0, zone Movable 4 5\n"
    assert memory_shape.parse_buddyinfo(text) == [4, 5]


def test_contiguous_counts_only_blocks_of_two_mib_or_more():
    # 4 KiB pages: order 9 is the first 2 MiB block.
    blocks = [0] * 9 + [3, 1]
    shape = memory_shape.summarize(4096, [256] + blocks[1:])

    assert shape["free_bytes"] == 256 * 4096 + 3 * 2 * MIB + 4 * MIB
    assert shape["contiguous_bytes"] == 3 * 2 * MIB + 4 * MIB
    assert shape["largest_block_bytes"] == 4 * MIB


def test_the_page_size_decides_what_an_order_means():
    # On a 64 KiB kernel, order 5 is already 2 MiB.
    shape = memory_shape.summarize(65536, [0, 0, 0, 0, 0, 1])
    assert shape["contiguous_bytes"] == 2 * MIB
    assert shape["contiguous_percent"] == 100.0


def test_the_spark_as_read_is_not_called_fragmented():
    shape = memory_shape.summarize(4096, memory_shape.parse_buddyinfo(SPARK))
    assert shape["contiguous_percent"] == 52.0
    assert shape["largest_block_bytes"] == 32 * MIB
    assert shape["fragmented"] is False


def test_free_memory_in_small_pieces_is_fragmented():
    # 2 GiB free, all of it in 1 MiB blocks (order 8 at 4 KiB).
    shape = memory_shape.summarize(4096, [0] * 8 + [2048])
    assert shape["contiguous_bytes"] == 0
    assert shape["fragmented"] is True


def test_too_little_free_memory_says_nothing_about_its_shape():
    # 512 MiB, all small: a node whose cache holds the rest, not a shredded one.
    shape = memory_shape.summarize(4096, [0] * 8 + [512])
    assert shape["fragmented"] is False


def test_no_counts_or_no_page_size_is_no_reading():
    assert memory_shape.summarize(4096, []) is None
    assert memory_shape.summarize(0, [1, 2]) is None
