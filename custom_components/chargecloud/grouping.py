"""Helpers for assigning Chargecloud EVSEs to Home Assistant devices."""

from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
from math import asin, cos, radians, sin, sqrt
from typing import TYPE_CHECKING, Any, Iterable
import unicodedata

if TYPE_CHECKING:
    from homeassistant.config_entries import ConfigEntry

ADDRESS_FIELDS = ("address", "postal_code", "city", "country")
GROUPING_MODE_AUTO = "automatic"
GROUPING_MODE_SEPARATE = "separate"
OPTION_GROUPING_MODE = "grouping_mode"
OPTION_MERGE_WITH = "merge_with"
MAX_GROUP_DISTANCE_METERS = 25


@dataclass(frozen=True)
class StationMetadata:
    """Stable fields used to identify a Chargecloud location."""

    provider_id: str | None
    location_id: str | None
    address_key: str | None
    latitude: float | None
    longitude: float | None
    name: str | None

    def __post_init__(self) -> None:
        """Normalize provider IDs consistently across saved entries."""
        if self.provider_id is not None:
            object.__setattr__(
                self, "provider_id", self.provider_id.strip().casefold()
            )

    @classmethod
    def from_location(cls, location: Any) -> StationMetadata:
        """Create metadata from a pychargecloud Location."""
        operator = getattr(location, "operator", None)
        provider_id = _clean_string(getattr(operator, "operator_id", None))
        if provider_id is not None:
            provider_id = provider_id.casefold()

        location_id = _clean_string(getattr(location, "id", None))
        address_key = normalized_address(
            *(getattr(location, field, None) for field in ADDRESS_FIELDS)
        )
        coordinates = getattr(location, "coordinates", None)
        latitude = _as_float(getattr(coordinates, "latitude", None))
        longitude = _as_float(getattr(coordinates, "longitude", None))

        return cls(
            provider_id=provider_id,
            location_id=location_id,
            address_key=address_key,
            latitude=latitude,
            longitude=longitude,
            name=_clean_string(getattr(location, "name", None)),
        )

    @classmethod
    def from_config_data(cls, data: dict[str, Any]) -> StationMetadata | None:
        """Read saved location metadata from a config entry."""
        if "provider_id" not in data:
            return None
        return cls(
            provider_id=_clean_string(data.get("provider_id")),
            location_id=_clean_string(data.get("location_id")),
            address_key=_clean_string(data.get("address_key")),
            latitude=_as_float(data.get("latitude")),
            longitude=_as_float(data.get("longitude")),
            name=_clean_string(data.get("location_name")),
        )

    def as_config_data(self) -> dict[str, Any]:
        """Serialize metadata for persistence in a config entry."""
        return {
            "provider_id": self.provider_id,
            "location_id": self.location_id,
            "address_key": self.address_key,
            "latitude": self.latitude,
            "longitude": self.longitude,
            "location_name": self.name,
        }


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
    """Normalize the full postal address for conservative comparisons."""
    address = " ".join(str(part).strip() for part in parts if part)
    if not address:
        return None

    decomposed = unicodedata.normalize("NFKD", address).casefold()
    normalized = "".join(
        character
        if character.isalnum()
        else " "
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
    """Return match priority and distance, or None when locations differ.

    The provider is required for every automatic match. Location ID wins over
    address, and address wins over proximity.
    """
    if not first.provider_id or first.provider_id != second.provider_id:
        return None

    if first.location_id and first.location_id == second.location_id:
        return (0, 0.0)

    if first.address_key and first.address_key == second.address_key:
        return (1, 0.0)

    distance = distance_meters(first, second)
    if distance is not None and distance <= MAX_GROUP_DISTANCE_METERS:
        return (2, distance)

    return None


def default_device_group_id(metadata: StationMetadata, evse_id: str) -> str:
    """Create a stable group ID when no configured location matches."""
    if metadata.provider_id and metadata.location_id:
        return f"location:{metadata.provider_id}:{metadata.location_id}"

    if metadata.provider_id and metadata.address_key:
        source = f"{metadata.provider_id}:{metadata.address_key}"
        return f"address:{sha256(source.encode()).hexdigest()[:20]}"

    if (
        metadata.provider_id
        and metadata.latitude is not None
        and metadata.longitude is not None
    ):
        source = (
            f"{metadata.provider_id}:{metadata.latitude:.5f}:"
            f"{metadata.longitude:.5f}"
        )
        return f"coordinates:{sha256(source.encode()).hexdigest()[:20]}"

    return f"evse:{evse_id}"


def find_device_group_id(
    metadata: StationMetadata,
    evse_id: str,
    entries: Iterable[ConfigEntry],
    *,
    exclude_entry_id: str | None = None,
) -> str:
    """Reuse the group ID of the best automatic match, if one exists."""
    matches: list[tuple[int, float, str, str]] = []
    for entry in entries:
        if entry.entry_id == exclude_entry_id:
            continue
        if entry.options.get(OPTION_GROUPING_MODE) == GROUPING_MODE_SEPARATE:
            continue

        other_metadata = StationMetadata.from_config_data(entry.data)
        if other_metadata is None:
            continue

        rank = location_match_rank(metadata, other_metadata)
        if rank is None:
            continue

        group_id = entry.data.get("device_group_id") or default_device_group_id(
            other_metadata, entry.data.get("evse_id", "unknown")
        )
        matches.append((*rank, entry.entry_id, group_id))

    if matches:
        return min(matches)[3]
    return default_device_group_id(metadata, evse_id)


def device_group_id_for_entry(
    entry: ConfigEntry, entries: Iterable[ConfigEntry]
) -> str:
    """Resolve automatic and manual grouping for an entity's config entry."""
    if entry.options.get(OPTION_GROUPING_MODE) == GROUPING_MODE_SEPARATE:
        return f"separate:{entry.entry_id}"

    all_entries = list(entries)
    by_evse_id = {
        other.data.get("evse_id"): other
        for other in all_entries
        if other.data.get("evse_id")
    }
    merge_graph: dict[str, set[str]] = {}
    manual_sources: set[str] = set()

    # Build automatic groups from metadata as well as persisted IDs. This also
    # handles entries first created at the same time before either could see
    # the other's metadata.
    automatic_entries = [
        other
        for other in all_entries
        if other.options.get(OPTION_GROUPING_MODE) != GROUPING_MODE_SEPARATE
        and other.data.get("evse_id")
    ]
    for index, first in enumerate(automatic_entries):
        first_evse_id = first.data["evse_id"]
        first_metadata = StationMetadata.from_config_data(first.data)
        for second in automatic_entries[index + 1 :]:
            second_evse_id = second.data["evse_id"]
            same_saved_group = (
                first.data.get("device_group_id")
                and first.data.get("device_group_id")
                == second.data.get("device_group_id")
            )
            second_metadata = StationMetadata.from_config_data(second.data)
            same_location = (
                first_metadata is not None
                and second_metadata is not None
                and location_match_rank(first_metadata, second_metadata) is not None
            )
            if same_saved_group or same_location:
                merge_graph.setdefault(first_evse_id, set()).add(second_evse_id)
                merge_graph.setdefault(second_evse_id, set()).add(first_evse_id)

    for source in all_entries:
        source_evse_id = source.data.get("evse_id")
        if (
            not source_evse_id
            or source.options.get(OPTION_GROUPING_MODE) == GROUPING_MODE_SEPARATE
        ):
            continue
        for target_evse_id in source.options.get(OPTION_MERGE_WITH, []):
            target = by_evse_id.get(target_evse_id)
            if (
                target is None
                or target.options.get(OPTION_GROUPING_MODE)
                == GROUPING_MODE_SEPARATE
            ):
                continue
            merge_graph.setdefault(source_evse_id, set()).add(target_evse_id)
            merge_graph.setdefault(target_evse_id, set()).add(source_evse_id)
            manual_sources.add(source_evse_id)

    current_evse_id = entry.data.get("evse_id")
    if current_evse_id in merge_graph:
        component = {current_evse_id}
        pending = [current_evse_id]
        while pending:
            current = pending.pop()
            for neighbor in merge_graph.get(current, set()) - component:
                component.add(neighbor)
                pending.append(neighbor)

        component_manual_sources = component & manual_sources
        if component_manual_sources:
            stable_entry_id = min(
                by_evse_id[evse_id].entry_id for evse_id in component_manual_sources
            )
            return f"manual:{stable_entry_id}"

        group_ids = {
            by_evse_id[evse_id].data.get("device_group_id")
            for evse_id in component
            if by_evse_id[evse_id].data.get("device_group_id")
        }
        if group_ids:
            return min(group_ids)

    return entry.data.get("device_group_id") or default_device_group_id(
        StationMetadata.from_config_data(entry.data)
        or StationMetadata(None, None, None, None, None, None),
        current_evse_id or entry.entry_id,
    )
