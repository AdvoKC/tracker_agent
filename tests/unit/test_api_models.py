from __future__ import annotations

from datetime import date
from typing import Any

import pytest
from pydantic import TypeAdapter, ValidationError

from promise_monitor.api.models import MeterPage, MeterSummary, RingState
from tests.fakes import ROUTES, load_fixture

PAGE_FIXTURES = [
    "api_presidential.json",
    "api_legislative_upper-chamber.json",
    "api_legislative_lower-chamber.json",
    "api_state_edo.json",
    "api_state_osun.json",
    "api_state_ekiti.json",
    "api_state_abuja_empty.json",
    "api_meter_ekiti_ring_archived.json",
]


@pytest.mark.parametrize("name", PAGE_FIXTURES)
def test_every_page_fixture_parses(name: str) -> None:
    page = MeterPage.model_validate(load_fixture(name)["data"])
    for m in page.all_meters:
        assert m.name


@pytest.mark.parametrize("name", [v for k, v in ROUTES.items() if "tenures" in k])
def test_tenure_fixtures_parse(name: str) -> None:
    items = TypeAdapter(list[MeterSummary]).validate_python(load_fixture(name)["data"])
    assert all(m.promises_count is not None for m in items)


def test_ring_states_fixture() -> None:
    states = TypeAdapter(list[RingState]).validate_python(
        load_fixture("api_all_promise-ring_states.json")["data"]
    )
    assert len(states) == 37
    ekiti = next(s for s in states if s.name == "Ekiti")
    assert {m.is_archived for m in ekiti.meter} == {True, False}


def test_current_meter_has_type_and_slug() -> None:
    page = MeterPage.model_validate(load_fixture("api_presidential.json")["data"])
    assert page.current_meter is not None
    assert page.current_meter.meter_type == "promise_tracker"
    assert page.current_meter.slug == "president-bola-ahmed-tinubu-2023-2027"
    assert page.promise_count == len(page.promises)


def test_empty_state_page() -> None:
    page = MeterPage.model_validate(load_fixture("api_state_abuja_empty.json")["data"])
    assert page.current_meter is None
    assert page.all_meters == []


def _summary(**over: Any) -> dict[str, Any]:
    base: dict[str, Any] = {"id": "e447b2f3-867c-4f12-a598-521572567fe7", "name": "X"}
    base.update(over)
    return base


@pytest.mark.parametrize(
    ("raw", "expected"), [(0, False), (1, True), ("1", True), ("0", False), (None, False)]
)
def test_is_archived_flag_quirks(raw: object, expected: bool) -> None:
    assert MeterSummary.model_validate(_summary(is_archived=raw)).is_archived is expected


def test_garbage_flag_rejected() -> None:
    with pytest.raises(ValidationError):
        MeterSummary.model_validate(_summary(is_archived="maybe"))


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("1970-01-01", None),
        ("null", None),
        ("None", None),
        ("", None),
        ("2023-05-29", date(2023, 5, 29)),
        ("2025-11-11 00:13:27", date(2025, 11, 11)),
        ("2024-10-11T10:08:47.000000Z", date(2024, 10, 11)),
    ],
)
def test_date_quirks(raw: str, expected: date | None) -> None:
    assert (
        MeterSummary.model_validate(_summary(tenure_date_start=raw)).tenure_date_start == expected
    )


def test_string_counts_and_nullish_strings() -> None:
    m = MeterSummary.model_validate(
        _summary(promises_count="19", meter_name="null", parent_meter_id="None")
    )
    assert m.promises_count == 19
    assert m.meter_name is None
    assert m.parent_meter_id is None


def test_missing_required_field_fails() -> None:
    with pytest.raises(ValidationError):
        MeterSummary.model_validate({"name": "no id"})


def test_staff_fields_not_modelled() -> None:
    page = MeterPage.model_validate(load_fixture("api_presidential.json")["data"])
    assert page.current_meter is not None
    assert "admin_id" not in page.current_meter.model_dump()
