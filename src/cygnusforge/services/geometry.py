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
# Orientations this small are rounding noise, so the point counts as on the line.
_ON_LINE = 1e-9
# An outline whose area is this small a fraction of its size has no area.
_FLAT = 1e-9
# How far either side of an outline edge is probed, in km, to tell whether the edge is a seam
# between two covered areas or the end of the covered ground.
_SEAM_KM = 0.01


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


def _side(a: Point, b: Point, c: Point) -> int:
    """Which side of line ab the point c is on: 1 or -1, or 0 if it is on the line."""
    value = _orientation(a, b, c)
    if abs(value) <= _ON_LINE:
        return 0
    return 1 if value > 0 else -1


def _crosses(a: Point, b: Point, c: Point, d: Point) -> bool:
    """True if segment ab and segment cd cross at a point that is inside both of them."""
    return _side(c, d, a) * _side(c, d, b) < 0 and _side(a, b, c) * _side(a, b, d) < 0


def clearance(point: Point, polygons: Sequence[Sequence[Point]]) -> float:
    """How far the point is from the nearest place where the covered ground ends: the edge of
    the map, or an outline bordering blank ground. Outlines that touch each other are not ends.
    0.0 if the point is not covered at all."""
    if not _covered(point, polygons):
        return 0.0
    ends = [
        (start, end)
        for polygon in polygons
        for start, end in _edges(polygon)
        if _ends_coverage(start, end, polygons)
    ]
    return min((_distance_to_segment(point, start, end) for start, end in ends), default=math.inf)


def _covered(point: Point, polygons: Sequence[Sequence[Point]]) -> bool:
    return any(contains(point, polygon) for polygon in polygons)


def _ends_coverage(start: Point, end: Point, polygons: Sequence[Sequence[Point]]) -> bool:
    """True if one side of the edge is covered and the other side is not."""
    (x1, y1), (x2, y2) = start, end
    length = math.hypot(x2 - x1, y2 - y1)
    if length == 0:
        return False
    normal_x, normal_y = -(y2 - y1) / length * _SEAM_KM, (x2 - x1) / length * _SEAM_KM
    mid_x, mid_y = (x1 + x2) / 2, (y1 + y2) / 2
    left = _covered((mid_x + normal_x, mid_y + normal_y), polygons)
    right = _covered((mid_x - normal_x, mid_y - normal_y), polygons)
    return left != right


def _distance_to_segment(point: Point, start: Point, end: Point) -> float:
    (px, py), (x1, y1), (x2, y2) = point, start, end
    dx, dy = x2 - x1, y2 - y1
    length_sq = dx * dx + dy * dy
    t = 0.0 if length_sq == 0 else max(0.0, min(1.0, ((px - x1) * dx + (py - y1) * dy) / length_sq))
    return math.hypot(px - (x1 + t * dx), py - (y1 + t * dy))


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
            if contains((x, y), other):
                return True
    return False


def contains(point: Point, polygon: Sequence[Point]) -> bool:
    """True if the point lies inside the polygon. A point on the outline may go either way."""
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
