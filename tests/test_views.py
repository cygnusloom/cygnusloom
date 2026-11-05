"""Focused tests for the read-only view builders in `services.views`."""

from __future__ import annotations

from cygnusloom.content.library import ClockRules
from cygnusloom.services.events import CampaignState, Clock
from cygnusloom.services.views import TimeView, time_view


def test_time_view_reports_the_day_part_and_night() -> None:
    clock = ClockRules(parts=("morning", "afternoon", "night"), stale_days=1)
    state = CampaignState(clock=Clock(day=3, part=2))

    assert time_view(state, clock) == TimeView(day=3, part="night", night=True)


def test_time_view_is_none_without_a_clock_or_parts() -> None:
    assert time_view(CampaignState(), None) is None
    assert time_view(CampaignState(), ClockRules(parts=(), stale_days=1)) is None
