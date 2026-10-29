import pytest

from cygnusforge.services.geometry import (
    clearance,
    contains,
    overlaps,
    polygon_problem,
    signed_area,
    uncovered,
)

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


def test_a_vertex_touching_an_edge_is_not_an_overlap():
    triangle = [(2.0, 0.5), (4.0, 0.0), (4.0, 2.0)]
    assert not overlaps(SQUARE, triangle)
    assert not overlaps(triangle, SQUARE)


def test_contains_is_true_inside_and_false_outside():
    assert contains((1.0, 1.0), SQUARE)
    assert not contains((3.0, 1.0), SQUARE)


def test_clearance_is_the_distance_to_the_nearest_edge():
    assert clearance((0.5, 1.0), [SQUARE]) == pytest.approx(0.5)


def test_clearance_is_zero_off_the_ground():
    assert clearance((5.0, 5.0), [SQUARE]) == 0.0


def test_neighbours_sharing_an_edge_are_not_an_end_of_ground():
    east = shifted(SQUARE, 2.0, 0.0)
    assert clearance((1.0, 1.0), [SQUARE, east]) == pytest.approx(1.0)
    assert clearance((1.9, 0.5), [SQUARE, east]) == pytest.approx(0.5)


def test_with_no_other_ground_the_whole_outline_is_open():
    cut = uncovered(SQUARE, [])
    assert cut.holed == 0
    assert len(cut.parts) == 1
    assert signed_area(cut.parts[0]) == pytest.approx(4.0)


def test_uncovered_keeps_only_the_ground_no_other_outline_covers():
    overlap = shifted(SQUARE, 1.0, 1.0)
    cut = uncovered(SQUARE, [overlap])
    assert cut.holed == 0
    assert len(cut.parts) == 1
    part = cut.parts[0]
    assert not overlaps(part, overlap)
    assert contains((0.5, 0.5), part)
    assert not contains((2.5, 2.5), part)
    assert abs(signed_area(part)) == pytest.approx(3.0)
    # The cut follows the other outline's edges, so it gains corners.
    assert len(part) > len(SQUARE)


def test_an_outline_inside_another_leaves_a_hole_and_no_open_part():
    inner = [(0.5, 0.5), (1.0, 0.5), (1.0, 1.0), (0.5, 1.0)]
    cut = uncovered(SQUARE, [inner])
    assert cut.parts == []
    assert cut.holed == 1


def test_a_cut_across_the_outline_returns_the_largest_part_first():
    band = [(0.5, 0.0), (0.9, 0.0), (0.9, 2.0), (0.5, 2.0)]
    cut = uncovered(SQUARE, [band])
    assert cut.holed == 0
    assert len(cut.parts) == 2
    assert contains((1.5, 1.0), cut.parts[0])
    assert contains((0.25, 1.0), cut.parts[1])
    assert abs(signed_area(cut.parts[0])) == pytest.approx(2.2)
    assert abs(signed_area(cut.parts[1])) == pytest.approx(1.0)
