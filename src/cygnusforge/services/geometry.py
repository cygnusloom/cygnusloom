"""Plane geometry for map regions: whether an outline is usable, and whether two outlines overlap.

Outlines are closed polygons given as points in order around the edge. Points are (x, y) pairs of
kilometers from the map's center. Touching along an edge or at a corner is not overlap.
"""

from __future__ import annotations

import math
from collections.abc import Iterator, Sequence

Point = tuple[float, float]

# Fractions along each edge where the overlap test samples a point just inside the outline.
_SAMPLES = (0.25, 0.5, 0.75)
# How far inside an outline a sample sits, as a fraction of the size of the two outlines.
_INSET = 1e-7
# An outline whose area is this small a fraction of its size has no area.
_FLAT = 1e-9


def polygon_problem(points: Sequence[Point]) -> str | None:
    """Why these points cannot outline a region, or None if they can."""
    if len(points) < 3:
        return "needs at least three points"
    if any(not all(math.isfinite(value) for value in point) for point in points):
        return "its points must be numbers"
    edges = list(_edges(points))
    if any(start == end for start, end in edges):
        return "repeats a point in a row"
    size = _size(points)
    if abs(signed_area(points)) <= _FLAT * size * size:
        return "has no area: its points lie on one line"
    last = len(edges) - 1
    for i in range(len(edges)):
        for j in range(i + 2, len(edges)):
            if i == 0 and j == last:
                continue
            if _crosses(*edges[i], *edges[j]):
                return "its edges cross each other"
    return None


def overlaps(first: Sequence[Point], second: Sequence[Point]) -> bool:
    """True if the two outlines share any area. Outlines that only touch do not overlap."""
    first_edges = list(_edges(first))
    second_edges = list(_edges(second))
    for a_start, a_end in first_edges:
        for b_start, b_end in second_edges:
            if _crosses(a_start, a_end, b_start, b_end):
                return True
    return _pokes_into(first, second) or _pokes_into(second, first)


def signed_area(points: Sequence[Point]) -> float:
    """Positive when the points run counterclockwise, negative when clockwise."""
    total = 0.0
    for (x1, y1), (x2, y2) in _edges(points):
        total += x1 * y2 - x2 * y1
    return total / 2


def _edges(points: Sequence[Point]) -> Iterator[tuple[Point, Point]]:
    count = len(points)
    for index in range(count):
        yield points[index], points[(index + 1) % count]


def _orientation(a: Point, b: Point, c: Point) -> float:
    return (b[0] - a[0]) * (c[1] - a[1]) - (b[1] - a[1]) * (c[0] - a[0])


def _crosses(a: Point, b: Point, c: Point, d: Point) -> bool:
    """True if segment ab and segment cd cross at a point that is inside both of them."""
    return _orientation(c, d, a) * _orientation(c, d, b) < 0 and (
        _orientation(a, b, c) * _orientation(a, b, d) < 0
    )


def _pokes_into(outline: Sequence[Point], other: Sequence[Point]) -> bool:
    """True if a point just inside the edge of outline lies inside other."""
    inset = _INSET * _size(list(outline) + list(other))
    # The inside of a counterclockwise outline is on its left; of a clockwise one, on its right.
    turn = 1.0 if signed_area(outline) > 0 else -1.0
    for (x1, y1), (x2, y2) in _edges(outline):
        dx, dy = x2 - x1, y2 - y1
        length = math.hypot(dx, dy)
        normal_x, normal_y = -dy / length * turn, dx / length * turn
        for fraction in _SAMPLES:
            x = x1 + dx * fraction + normal_x * inset
            y = y1 + dy * fraction + normal_y * inset
            if _inside((x, y), other):
                return True
    return False


def _inside(point: Point, polygon: Sequence[Point]) -> bool:
    """Ray casting. Only used for points kept off the outline, so the boundary never matters."""
    x, y = point
    inside = False
    for (x1, y1), (x2, y2) in _edges(polygon):
        if (y1 > y) != (y2 > y):
            crossing = x1 + (y - y1) * (x2 - x1) / (y2 - y1)
            if x < crossing:
                inside = not inside
    return inside


def _size(points: Sequence[Point]) -> float:
    xs = [x for x, _ in points]
    ys = [y for _, y in points]
    return max(max(xs) - min(xs), max(ys) - min(ys), 1.0)
