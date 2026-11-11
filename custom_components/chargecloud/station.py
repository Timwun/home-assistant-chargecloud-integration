"""Station identity helpers for Chargecloud locations."""

from __future__ import annotations

import unicodedata
from dataclasses import dataclass
from hashlib import sha256
from math import asin, cos, radians, sin, sqrt
from typing import TYPE_CHECKING, Any, Iterable

if TYPE_CHECKING:
    from homeassistant.config_entries import ConfigEntry

MAX_STATION_DISTANCE_METERS = 25
OPTION_SPLIT_EVSE_IDS = "split_evse_ids"
OPTION_MERGE_STATIONS = "merge_stations"


@dataclass(frozen=True)
class StationMetadata:
    """Location fields used to identify and name a station."""

    provider_id: str | None
    location_id: str | None
    address: str | None
    postal_code: str | None
    city: str | None
    country: str | None
    address_key: str | None
    latitude: float | None
    longitude: float | None
    name: str | None

    @classmethod
    def from_location(cls, location: Any) -> StationMetadata:
        """Create metadata from a pychargecloud Location."""
        operator = getattr(location, "operator", None)
        provider_id = _clean_string(getattr(operator, "operator_id", None))
        address = _clean_string(getattr(location, "address", None))
        postal_code = _clean_string(getattr(location, "postal_code", None))
        city = _clean_string(getattr(location, "city", None))
        country = _clean_string(getattr(location, "country", None))
        coordinates = getattr(location, "coordinates", None)

        return cls(
            provider_id=provider_id,
            location_id=_clean_string(getattr(location, "id", None)),
            address=address,
            postal_code=postal_code,
            city=city,
            country=country,
            address_key=normalized_address(address, postal_code, city, country),
            latitude=_as_float(getattr(coordinates, "latitude", None)),
            longitude=_as_float(getattr(coordinates, "longitude", None)),
            name=_clean_string(getattr(location, "name", None)),
        )

    @classmethod
    def from_config_data(cls, data: dict[str, Any]) -> StationMetadata | None:
        """Read saved location metadata from a config entry."""
        if "provider_id" not in data:
            return None

        address = _clean_string(data.get("address"))
        postal_code = _clean_string(data.get("postal_code"))
        city = _clean_string(data.get("city"))
        country = _clean_string(data.get("country"))
        return cls(
            provider_id=_clean_string(data.get("provider_id")),
            location_id=_clean_string(data.get("location_id")),
            address=address,
            postal_code=postal_code,
            city=city,
            country=country,
            address_key=_clean_string(data.get("address_key"))
            or normalized_address(address, postal_code, city, country),
            latitude=_as_float(data.get("latitude")),
            longitude=_as_float(data.get("longitude")),
            name=_clean_string(data.get("location_name")),
        )

    def as_config_data(self) -> dict[str, Any]:
        """Serialize metadata for persistence in a config entry."""
        return {
            "provider_id": self.provider_id,
            "location_id": self.location_id,
            "address": self.address,
            "postal_code": self.postal_code,
            "city": self.city,
            "country": self.country,
            "address_key": self.address_key,
            "latitude": self.latitude,
            "longitude": self.longitude,
            "location_name": self.name,
        }

    @property
    def device_name(self) -> str:
        """Format the station name without repeating an address already in it."""
        address = self.formatted_address
        location_name = self.name
        if address and location_name:
            address_line = normalized_address(self.address)
            normalized_name = normalized_address(location_name)
            if (
                not address_line
                or not normalized_name
                or address_line not in normalized_name
            ):
                location_name = f"{location_name} / {address}"
        elif address:
            location_name = address

        parts = [part for part in (self.provider_id, location_name) if part]
        return " - ".join(parts) or "Chargecloud station"

    @property
    def formatted_address(self) -> str | None:
        """Return the address with available locality information."""
        line = self.address
        locality = " ".join(
            part for part in (self.postal_code, self.city) if part
        )
        parts = [part for part in (line, locality, self.country) if part]
        return ", ".join(parts) or None


def _clean_string(value: Any) -> str | None:
    if value is None:
        return None
    result = str(value).strip()
    return result or None


def _as_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def normalized_address(*parts: Any) -> str | None:
    """Normalize address fields for matching."""
    address = " ".join(str(part).strip() for part in parts if part)
    if not address:
        return None

    decomposed = unicodedata.normalize("NFKD", address).casefold()
    normalized = "".join(
        character if character.isalnum() else " "
        for character in decomposed
        if not unicodedata.combining(character)
    )
    return " ".join(normalized.split()) or None


def distance_meters(first: StationMetadata, second: StationMetadata) -> float | None:
    """Return the great-circle distance between two locations."""
    if (
        first.latitude is None
        or first.longitude is None
        or second.latitude is None
        or second.longitude is None
    ):
        return None

    latitude_delta = radians(second.latitude - first.latitude)
    longitude_delta = radians(second.longitude - first.longitude)
    latitude_first = radians(first.latitude)
    latitude_second = radians(second.latitude)
    haversine = (
        sin(latitude_delta / 2) ** 2
        + cos(latitude_first)
        * cos(latitude_second)
        * sin(longitude_delta / 2) ** 2
    )
    return 6_371_000 * 2 * asin(sqrt(min(1.0, haversine)))


def location_match_rank(
    first: StationMetadata, second: StationMetadata
) -> tuple[int, float] | None:
    """Return match priority and distance, or None when stations differ.

    Provider IDs must agree. Location ID is primary, address is second, and
    proximity is the final fallback.
    """
    if (
        not first.provider_id
        or not second.provider_id
        or first.provider_id.casefold() != second.provider_id.casefold()
    ):
        return None

    if first.location_id and first.location_id == second.location_id:
        return (0, 0.0)

    if first.address_key and first.address_key == second.address_key:
        return (1, 0.0)

    distance = distance_meters(first, second)
    if distance is not None and distance <= MAX_STATION_DISTANCE_METERS:
        return (2, distance)

    return None


def station_device_identifier(metadata: StationMetadata, evse_id: str) -> str:
    """Create a stable station identifier from provider/location metadata."""
    provider_id = metadata.provider_id.casefold() if metadata.provider_id else None
    if provider_id and metadata.location_id:
        return f"location:{provider_id}:{metadata.location_id}"

    if provider_id and metadata.address_key:
        source = f"{provider_id}:{metadata.address_key}"
        return f"address:{sha256(source.encode()).hexdigest()[:20]}"

    if provider_id and metadata.latitude is not None and metadata.longitude is not None:
        source = f"{provider_id}:{metadata.latitude:.5f}:{metadata.longitude:.5f}"
        return f"coordinates:{sha256(source.encode()).hexdigest()[:20]}"

    return f"evse:{evse_id}"


def configured_evse_ids(data: dict[str, Any]) -> list[str]:
    """Read EVSE IDs from current and legacy config-entry formats."""
    ids = list(data.get("evse_ids", []))
    if data.get("evse_id"):
        ids.append(data["evse_id"])
    return list(dict.fromkeys(evse_id for evse_id in ids if evse_id))


def find_station_entries(
    metadata: StationMetadata,
    entries: Iterable[ConfigEntry],
    *,
    exclude_entry_id: str | None = None,
) -> list[ConfigEntry]:
    """Return matching entries, ordered by location-ID/address/distance match."""
    matches: list[tuple[int, float, str, ConfigEntry]] = []
    for entry in entries:
        if entry.entry_id == exclude_entry_id:
            continue
        other_metadata = StationMetadata.from_config_data(entry.data)
        if other_metadata is None:
            continue
        rank = location_match_rank(metadata, other_metadata)
        if rank is not None:
            matches.append((*rank, entry.entry_id, entry))
    return [match[3] for match in sorted(matches, key=lambda match: match[:3])]
