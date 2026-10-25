import pytest

from cygnusforge.services.geometry import overlaps, polygon_problem, signed_area

SQUARE = [(0.0, 0.0), (2.0, 0.0), (2.0, 2.0), (0.0, 2.0)]
CLOCKWISE_SQUARE = list(reversed(SQUARE))


def shifted(points, dx, dy):
    return [(x + dx, y + dy) for x, y in points]


def test_square_is_a_valid_region_in_either_direction():
    assert polygon_problem(SQUARE) is None
    assert polygon_problem(CLOCKWISE_SQUARE) is None


def test_signed_area_tells_the_direction_apart():
    assert signed_area(SQUARE) == pytest.approx(4.0)
    assert signed_area(CLOCKWISE_SQUARE) == pytest.approx(-4.0)


@pytest.mark.parametrize(
    ("points", "problem"),
    [
        ([(0, 0), (1, 0)], "needs at least three points"),
        ([(0, 0), (1, 0), (float("nan"), 1)], "its points must be numbers"),
        ([(0, 0), (0, 0), (1, 0), (1, 1)], "repeats a point in a row"),
        ([(0, 0), (1, 1), (2, 2)], "has no area: its points lie on one line"),
        # A bow-tie with unequal lobes: it has area, but one edge crosses another.
        ([(0, 0), (4, 4), (4, 0), (0, 1)], "its edges cross each other"),
    ],
)
def test_bad_outlines_are_refused_with_a_reason(points, problem):
    assert polygon_problem(points) == problem


def test_partial_overlap_is_overlap():
    assert overlaps(SQUARE, shifted(SQUARE, 1.0, 1.0))


def test_one_inside_the_other_is_overlap_both_ways():
    inner = shifted(SQUARE, 0.5, 0.5)
    inner = [(x * 0.5, y * 0.5) for x, y in inner]
    assert overlaps(SQUARE, inner)
    assert overlaps(inner, SQUARE)


def test_identical_outlines_overlap():
    assert overlaps(SQUARE, list(SQUARE))
    assert overlaps(SQUARE, CLOCKWISE_SQUARE)


def test_neighbours_sharing_an_edge_do_not_overlap():
    assert not overlaps(SQUARE, shifted(SQUARE, 2.0, 0.0))


def test_neighbours_sharing_a_corner_do_not_overlap():
    assert not overlaps(SQUARE, shifted(SQUARE, 2.0, 2.0))


def test_separate_shapes_do_not_overlap():
    assert not overlaps(SQUARE, shifted(SQUARE, 5.0, -1.0))


def test_overlap_is_symmetric_for_a_triangle_across_a_square():
    triangle = [(1.0, -1.0), (3.0, 1.0), (1.0, 3.0)]
    assert overlaps(SQUARE, triangle)
    assert overlaps(triangle, SQUARE)
