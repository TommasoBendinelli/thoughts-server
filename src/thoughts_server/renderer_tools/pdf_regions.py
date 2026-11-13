# SPDX-License-Identifier: GPL-2.0-or-later
# SPDX-FileCopyrightText: 2026 Tommaso Bendinelli
"""Read PDF regions in one bounded verification session per publication batch."""

from bisect import bisect_left, bisect_right
from collections import OrderedDict
from pathlib import Path

from . import page_extraction


class PdfRegions:
    """Serve page characters from the shared page extractor.

    Each distinct page is extracted once per process and, with a configured
    cache root, reused from the previous build when its content is unchanged.
    The session retains its eight most recent pages and their region indexes.
    """

    def __init__(self, root: Path, extractor=None):
        self.root = root
        self._extractor = extractor or page_extraction.default_extractor()
        self._characters = OrderedDict()
        self._indexes = OrderedDict()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self._characters.clear()
        self._indexes.clear()
        return False

    def characters(self, filename: str, page: int):
        key = (filename, page)
        cached = self._characters.get(key)
        if cached is not None:
            self._characters.move_to_end(key)
            return cached
        count = self._extractor.page_count(self.root / filename)
        if page < 1 or (count is not None and page > count):
            raise IndexError("PDF page is outside the publication")
        width, height, characters = self._extractor.pages(
            self.root / filename, "chars", [page], slot="publication:" + filename,
        )[page]
        value = (width, height, [tuple(character) for character in characters])
        self._characters[key] = value
        while len(self._characters) > 8:
            self._characters.popitem(last=False)
        return value

    def region_text(self, filename: str, page: int, regions) -> str:
        """Text of the characters centered in any region, in reading order.

        Equivalent to testing every character against every region: a
        vertical index narrows the candidates, then the same comparison decides.
        """
        key = (filename, page)
        index = self._indexes.get(key)
        if index is None:
            width, height, characters = self.characters(filename, page)
            centers = []
            for position, (x0, x1, top, bottom, _text) in enumerate(characters):
                x = (x0 + x1) / (2 * width)
                y = (top + bottom) / (2 * height)
                if y == y:  # NaN centers never fall inside a region.
                    centers.append((y, x, position))
            centers.sort(key=lambda value: (value[0], value[2]))
            index = (characters, [value[0] for value in centers], centers)
            self._indexes[key] = index
            while len(self._indexes) > 8:
                self._indexes.popitem(last=False)
        else:
            self._indexes.move_to_end(key)
        characters, ys, centers = index
        selected_positions = set()
        for left, region_top, right, region_bottom in regions:
            for y, x, position in centers[bisect_left(ys, region_top):bisect_right(ys, region_bottom)]:
                if left <= x <= right and region_top <= y <= region_bottom:
                    selected_positions.add(position)
        selected = [
            (characters[position][0], characters[position][2], characters[position][4])
            for position in sorted(selected_positions)
        ]
        selected.sort(key=lambda value: (round(value[1], 1), value[0]))
        return "".join(value[2] for value in selected)
