"""Plane geometry for map regions: whether an outline is usable, whether two outlines overlap, and
what open ground an outline leaves.

Outlines are closed polygons given as points in order around the edge. Points are (x, y) pairs of
kilometers from the map's center. Touching along an edge or at a corner is not overlap.
"""

from __future__ import annotations

import math
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from functools import lru_cache

from shapely.geometry import Point as ShapePoint
from shapely.geometry import Polygon
from shapely.ops import unary_union

Point = tuple[float, float]

# Fractions along each edge where the overlap test samples a point just inside the outline.
_SAMPLES = (0.25, 0.5, 0.75)
# How far inside an outline a sample sits, as a fraction of the size of the two outlines.
_INSET = 1e-7
# Orientations this small are rounding noise, so the point counts as on the line.
_ON_LINE = 1e-9
# An outline whose area is this small a fraction of its size has no area.
_FLAT = 1e-9
# A cut part with less area than this, in square kilometers, is an artifact of the arithmetic.
_EMPTY_KM2 = 1e-6
# Cut corners are kept to this many decimals, 0.1 m, so the values stay readable.
_PRECISION = 4


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


def representative_point(points: Sequence[Point]) -> Point:
    """A point inside the outline, for placing a label. It is near the middle but always on the
    ground, even for a crescent or another shape whose centroid falls outside."""
    point = Polygon(points).representative_point()
    return (float(point.x), float(point.y))


@dataclass(frozen=True)
class Cut:
    """What is left of an outline once the ground other outlines cover is taken away."""

    # Simple outlines with no holes in them, mostly open ground first.
    parts: list[list[Point]]
    # How many parts were left out because they have a hole, which a region cannot draw.
    holed: int = 0


def uncovered(points: Sequence[Point], others: Sequence[Sequence[Point]]) -> Cut:
    """The parts of an outline that lie on ground no other outline covers. Parts with no area are
    dropped, and parts with a hole in them are counted in `holed` instead of returned."""
    shape = Polygon(points)
    if others:
        shape = shape.difference(unary_union([Polygon(other) for other in others]))
    pieces = list(shape.geoms) if hasattr(shape, "geoms") else [shape]
    parts: list[list[Point]] = []
    holed = 0
    for piece in pieces:
        if piece.geom_type != "Polygon" or piece.area <= _EMPTY_KM2:
            continue
        if piece.interiors:
            holed += 1
            continue
        outline = [
            (round(float(x), _PRECISION), round(float(y), _PRECISION))
            for x, y in piece.exterior.coords[:-1]
        ]
        parts.append(outline)
    parts.sort(key=lambda part: abs(signed_area(part)), reverse=True)
    return Cut(parts=parts, holed=holed)


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
    if not polygons:
        return 0.0
    shape = _coverage_shape(tuple(tuple(polygon) for polygon in polygons))
    target = ShapePoint(point)
    if not shape.covers(target):
        return 0.0
    return float(target.distance(shape.boundary))


@lru_cache(maxsize=8)
def _coverage_shape(polygons: tuple[tuple[Point, ...], ...]) -> Polygon:
    """The union of the covered outlines. Cached because a map checks clearance for many points
    against the same ground, and the union is the expensive part."""
    return unary_union([Polygon(polygon) for polygon in polygons])


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
