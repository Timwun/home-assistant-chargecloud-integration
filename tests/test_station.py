"""Tests for Chargecloud station identity and EVSE ID validation."""

from __future__ import annotations

import importlib.util
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest

COMPONENT_PATH = Path(__file__).parents[1] / "custom_components" / "chargecloud"


def load_module(path: Path, name: str):
    """Load pure helpers without importing Home Assistant."""
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


station = load_module(COMPONENT_PATH / "station.py", "chargecloud_station")
evse_id_validation = load_module(
    COMPONENT_PATH / "evse_id.py", "chargecloud_evse_id"
)


def make_metadata(
    *,
    provider_id: str | None = "MVV",
    location_id: str | None = "100",
    address: str | None = "Main Street 1",
    postal_code: str | None = "12345",
    city: str | None = "Berlin",
    country: str | None = "DE",
    latitude: float | None = 52.0,
    longitude: float | None = 13.0,
    name: str | None = "Station",
):
    return station.StationMetadata.from_location(
        SimpleNamespace(
            id=location_id,
            name=name,
            address=address,
            postal_code=postal_code,
            city=city,
            country=country,
            operator=SimpleNamespace(operator_id=provider_id),
            coordinates=SimpleNamespace(latitude=latitude, longitude=longitude),
        )
    )


def make_entry(
    entry_id: str,
    evse_ids: list[str],
    metadata,
) -> SimpleNamespace:
    return SimpleNamespace(
        entry_id=entry_id,
        data={**metadata.as_config_data(), "evse_ids": evse_ids},
        options={},
    )


class StationTests(unittest.TestCase):
    """Check provider-scoped station matching and display identity."""

    def test_tank_e_location_metadata_and_device_name(self) -> None:
        metadata = make_metadata(
            provider_id="TNK",
            location_id="1814001",
            address="Geestemünderstraße 18",
            postal_code="50735",
            city="Köln",
            name="[TankE] - Geestemünderstraße 18",
            latitude=50.998684,
            longitude=6.939453,
        )

        self.assertEqual(metadata.provider_id, "TNK")
        self.assertEqual(metadata.location_id, "1814001")
        self.assertEqual(
            metadata.device_name, "TNK - [TankE] - Geestemünderstraße 18"
        )
        self.assertEqual(
            station.station_device_identifier(metadata, "DE*TNK*E200826*01"),
            "location:tnk:1814001",
        )

    def test_tank_e_ports_resolve_to_one_station_entry(self) -> None:
        first_port = make_metadata(
            provider_id="TNK",
            location_id="1814001",
            address="Geestemünderstraße 18",
            postal_code="50735",
            city="Köln",
            name="[TankE] - Geestemünderstraße 18",
            latitude=50.998684,
            longitude=6.939453,
        )
        second_port = make_metadata(
            provider_id="TNK",
            location_id="1814001",
            address="Geestemünderstraße 18",
            postal_code="50735",
            city="Köln",
            name="[TankE] - Geestemünderstraße 18",
            latitude=50.998684,
            longitude=6.939453,
        )
        existing_entry = make_entry(
            "station-entry", ["DE*TNK*E200826*01"], first_port
        )

        self.assertEqual(
            [entry.entry_id for entry in station.find_station_entries(
                second_port, [existing_entry]
            )],
            ["station-entry"],
        )

    def test_location_id_match_is_provider_scoped(self) -> None:
        first = make_metadata(provider_id="MVV", location_id="42")
        same_station = make_metadata(provider_id="mvv", location_id="42")
        other_provider = make_metadata(
            provider_id="ENBW", location_id="42", address=first.address
        )

        self.assertEqual(station.location_match_rank(first, same_station), (0, 0.0))
        self.assertIsNone(station.location_match_rank(first, other_provider))

    def test_address_is_second_match_rule(self) -> None:
        first = make_metadata(location_id="1")
        second = make_metadata(
            location_id="2",
            address=" main STREET 1 ",
            postal_code="12345",
            city="BERLIN",
        )

        self.assertEqual(station.location_match_rank(first, second), (1, 0.0))

    def test_coordinates_are_last_match_rule_and_limited_to_25_meters(self) -> None:
        first = make_metadata(
            address=None,
            postal_code=None,
            city=None,
            country=None,
            latitude=52.0,
        )
        within_range = make_metadata(
            location_id="2",
            address=None,
            postal_code=None,
            city=None,
            country=None,
            latitude=52.00018,
        )
        outside_range = make_metadata(
            location_id="3",
            address=None,
            postal_code=None,
            city=None,
            country=None,
            latitude=52.00030,
        )

        match = station.location_match_rank(first, within_range)
        self.assertIsNotNone(match)
        self.assertEqual(match[0], 2)
        self.assertLessEqual(match[1], 25)
        self.assertIsNone(station.location_match_rank(first, outside_range))

    def test_find_station_entries_prefers_location_id(self) -> None:
        target = make_metadata(location_id="42")
        address_match = make_entry(
            "address-entry",
            ["DE*MVV*E1*L1"],
            make_metadata(location_id="41"),
        )
        location_match = make_entry(
            "location-entry",
            ["DE*MVV*E2*R1"],
            make_metadata(location_id="42"),
        )

        self.assertEqual(
            [entry.entry_id for entry in station.find_station_entries(
                target, [address_match, location_match]
            )],
            ["location-entry", "address-entry"],
        )

    def test_configured_evse_ids_support_legacy_entries(self) -> None:
        self.assertEqual(
            station.configured_evse_ids(
                {"evse_id": "DE*TNK*E200826*01", "evse_ids": ["DE*TNK*E200826*02"]}
            ),
            ["DE*TNK*E200826*02", "DE*TNK*E200826*01"],
        )

    def test_tes_evse_id_is_valid_and_preserved_as_entered(self) -> None:
        evse_id = "DE*TES*EE0010321"

        self.assertTrue(evse_id_validation.is_valid_evse_id(evse_id))
        self.assertTrue(evse_id_validation.is_valid_evse_id(f"{evse_id}*R1"))


if __name__ == "__main__":
    unittest.main()
