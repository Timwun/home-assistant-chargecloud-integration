"""Tests for Chargecloud station grouping."""

from __future__ import annotations

import importlib.util
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest

GROUPING_PATH = (
    Path(__file__).parents[1]
    / "custom_components"
    / "chargecloud"
    / "grouping.py"
)


def load_module(path: Path, name: str):
    """Load a pure helper without importing the Home Assistant integration."""
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


grouping = load_module(GROUPING_PATH, "chargecloud_grouping")
evse_id_validation = load_module(
    GROUPING_PATH.with_name("evse_id.py"), "chargecloud_evse_id"
)

GROUPING_MODE_SEPARATE = grouping.GROUPING_MODE_SEPARATE
OPTION_GROUPING_MODE = grouping.OPTION_GROUPING_MODE
OPTION_MERGE_WITH = grouping.OPTION_MERGE_WITH
StationMetadata = grouping.StationMetadata
default_device_group_id = grouping.default_device_group_id
device_group_id_for_entry = grouping.device_group_id_for_entry
find_device_group_id = grouping.find_device_group_id
location_match_rank = grouping.location_match_rank


def make_metadata(
    *,
    provider_id: str | None = "mvv",
    location_id: str | None = "100",
    address_key: str | None = None,
    latitude: float | None = 52.0,
    longitude: float | None = 13.0,
    name: str | None = "Station",
) -> StationMetadata:
    return StationMetadata(
        provider_id,
        location_id,
        address_key,
        latitude,
        longitude,
        name,
    )


def make_entry(
    entry_id: str,
    evse_id: str,
    metadata: StationMetadata,
    group_id: str,
    options: dict | None = None,
) -> SimpleNamespace:
    return SimpleNamespace(
        entry_id=entry_id,
        data={
            "evse_id": evse_id,
            **metadata.as_config_data(),
            "device_group_id": group_id,
        },
        options=options or {},
    )


class StationGroupingTests(unittest.TestCase):
    """Verify location matching and EVSE grouping behavior."""

    def test_provider_location_metadata_is_extracted(self) -> None:
        location = SimpleNamespace(
            id=123,
            name="MVV Main Street",
            address="Main St.",
            postal_code="12345",
            city="Berlin",
            country="DE",
            operator=SimpleNamespace(operator_id="MVV"),
            coordinates=SimpleNamespace(latitude=52.0, longitude=13.0),
        )

        metadata = StationMetadata.from_location(location)

        self.assertEqual(metadata.provider_id, "mvv")
        self.assertEqual(metadata.location_id, "123")
        self.assertEqual(metadata.address_key, "main st 12345 berlin de")

    def test_location_id_is_primary_and_scoped_to_provider(self) -> None:
        station = make_metadata(provider_id="mvv", location_id="42")
        same_station = make_metadata(provider_id="MVV", location_id="42")
        other_provider = make_metadata(
            provider_id="enbw",
            location_id="42",
            address_key="main street 1 12345 berlin germany",
        )

        self.assertEqual(location_match_rank(station, same_station), (0, 0.0))
        self.assertIsNone(location_match_rank(station, other_provider))

    def test_address_is_used_after_location_id(self) -> None:
        first = make_metadata(
            location_id="1", address_key="main street 1 12345 berlin germany"
        )
        second = make_metadata(
            location_id="2",
            address_key="main street 1 12345 berlin germany",
            latitude=53.0,
            longitude=14.0,
        )

        self.assertEqual(location_match_rank(first, second), (1, 0.0))

    def test_coordinates_are_last_fallback_and_limited_to_25_meters(self) -> None:
        first = make_metadata(location_id="1", address_key=None)
        within_range = make_metadata(
            location_id="2", address_key=None, latitude=52.00018
        )
        outside_range = make_metadata(
            location_id="3", address_key=None, latitude=52.00030
        )

        match = location_match_rank(first, within_range)
        self.assertIsNotNone(match)
        self.assertEqual(match[0], 2)
        self.assertLessEqual(match[1], 25)
        self.assertIsNone(location_match_rank(first, outside_range))

    def test_sample_evse_id_is_not_normalized_for_grouping(self) -> None:
        metadata = make_metadata(provider_id="tes", location_id="location-7")
        evse_id = "DE*TES*EE0010321"

        self.assertTrue(evse_id_validation.is_valid_evse_id(evse_id))
        self.assertTrue(evse_id_validation.is_valid_evse_id(f"{evse_id}*R1"))
        self.assertEqual(
            default_device_group_id(metadata, evse_id),
            "location:tes:location-7",
        )

    def test_existing_provider_and_location_group_is_reused(self) -> None:
        metadata = make_metadata(provider_id="mvv", location_id="42")
        existing = make_entry(
            "entry-1", "DE*MVV*E1*L1", metadata, "existing-device-group"
        )

        self.assertEqual(
            find_device_group_id(
                metadata, "DE*MVV*E1*R1", [existing]
            ),
            "existing-device-group",
        )

    def test_address_fallback_joins_entries_with_different_location_ids(self) -> None:
        address = "main street 1 12345 berlin germany"
        first = make_entry(
            "entry-1",
            "DE*MVV*E1*L1",
            make_metadata(location_id="1", address_key=address),
            "group-1",
        )
        second = make_entry(
            "entry-2",
            "DE*MVV*E2*R1",
            make_metadata(location_id="2", address_key=address),
            "group-2",
        )

        self.assertEqual(
            device_group_id_for_entry(first, [first, second]),
            device_group_id_for_entry(second, [first, second]),
        )

    def test_coordinate_fallback_joins_entries_within_25_meters(self) -> None:
        first = make_entry(
            "entry-1",
            "DE*MVV*E1*L1",
            make_metadata(location_id="1", address_key=None),
            "group-1",
        )
        second = make_entry(
            "entry-2",
            "DE*MVV*E2*R1",
            make_metadata(location_id="2", address_key=None, latitude=52.00018),
            "group-2",
        )

        self.assertEqual(
            device_group_id_for_entry(first, [first, second]),
            device_group_id_for_entry(second, [first, second]),
        )

    def test_separate_override_gets_a_private_device_group(self) -> None:
        metadata = make_metadata()
        entry = make_entry(
            "entry-1",
            "DE*MVV*E1*L1",
            metadata,
            "automatic-group",
            {OPTION_GROUPING_MODE: GROUPING_MODE_SEPARATE},
        )

        self.assertEqual(device_group_id_for_entry(entry, [entry]), "separate:entry-1")

    def test_separate_override_splits_an_automatic_group(self) -> None:
        metadata = make_metadata()
        first = make_entry("entry-1", "DE*MVV*E1*L1", metadata, "shared-group")
        second = make_entry(
            "entry-2",
            "DE*MVV*E1*R1",
            metadata,
            "shared-group",
            {OPTION_GROUPING_MODE: GROUPING_MODE_SEPARATE},
        )

        self.assertEqual(
            device_group_id_for_entry(first, [first, second]), "shared-group"
        )
        self.assertEqual(
            device_group_id_for_entry(second, [first, second]), "separate:entry-2"
        )

    def test_manual_merge_links_configured_entries(self) -> None:
        metadata = make_metadata()
        first = make_entry(
            "entry-1",
            "DE*MVV*E1*L1",
            metadata,
            "group-1",
            {OPTION_MERGE_WITH: ["DE*MVV*E2*R1"]},
        )
        second = make_entry(
            "entry-2", "DE*MVV*E2*R1", metadata, "group-2"
        )
        sibling = make_entry(
            "entry-3", "DE*MVV*E1*R1", metadata, "group-1"
        )
        entries = [first, second, sibling]

        self.assertEqual(
            device_group_id_for_entry(first, entries),
            device_group_id_for_entry(second, entries),
        )
        self.assertEqual(
            device_group_id_for_entry(first, entries),
            device_group_id_for_entry(sibling, entries),
        )


if __name__ == "__main__":
    unittest.main()
