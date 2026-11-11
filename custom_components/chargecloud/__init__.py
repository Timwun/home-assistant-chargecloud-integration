"""The chargecloud integration."""
from __future__ import annotations

import asyncio
import logging

import async_timeout
import chargecloudapi

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator

from .const import DOMAIN, UPDATE_INTERVAL, EvseId
from .station import (
    OPTION_SPLIT_EVSE_IDS,
    StationMetadata,
    configured_evse_ids,
    station_device_identifier,
)

_LOGGER = logging.getLogger(__name__)

PLATFORMS: list[Platform] = [Platform.SENSOR]


async def async_migrate_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Migrate legacy one-EVSE entries to the station entry format."""
    if entry.version > 2:
        return False

    if entry.version == 1:
        evse_ids = configured_evse_ids(entry.data)
        data = dict(entry.data)
        metadata = StationMetadata.from_config_data(data)
        station_id = data.get("station_id") or data.get("device_group_id")
        if metadata is not None and evse_ids:
            station_id = station_id or station_device_identifier(
                metadata, evse_ids[0]
            )
            data.update(metadata.as_config_data())
        data["evse_ids"] = evse_ids
        if station_id:
            data["station_id"] = station_id
        data.pop("evse_id", None)
        data.pop("device_group_id", None)

        options = dict(entry.options)
        if options.pop("grouping_mode", None) == "separate" and evse_ids:
            options[OPTION_SPLIT_EVSE_IDS] = evse_ids
        options.pop("merge_with", None)
        hass.config_entries.async_update_entry(
            entry, data=data, options=options, version=2
        )
        _LOGGER.info("Migrated Chargecloud entry %s to station format", entry.entry_id)

    return True


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up chargecloud from a config entry."""

    hass.data.setdefault(DOMAIN, {})

    api = chargecloudapi.Api(websession=async_get_clientsession(hass))
    # missing: validate api connection

    evse_ids = configured_evse_ids(entry.data)
    if not evse_ids:
        _LOGGER.error("Config entry %s does not contain any EVSE IDs", entry.entry_id)
        return False

    coordinator = ChargeCloudDataUpdateCoordinator(hass, api, evse_ids)
    hass.data[DOMAIN][entry.entry_id] = coordinator
    await coordinator.async_config_entry_first_refresh()

    location = next(iter((coordinator.data or {}).values()), None)
    metadata = (
        StationMetadata.from_location(location)
        if location is not None
        else StationMetadata.from_config_data(entry.data)
    )
    if metadata is not None:
        station_id = (
            entry.data.get("station_id")
            or entry.data.get("device_group_id")
            or station_device_identifier(metadata, evse_ids[0])
        )
        updated_data = {
            **entry.data,
            **metadata.as_config_data(),
            "evse_ids": evse_ids,
            "station_id": station_id,
        }
        updated_data.pop("evse_id", None)
        updated_data.pop("device_group_id", None)
        if updated_data != entry.data:
            hass.config_entries.async_update_entry(entry, data=updated_data)

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    if unload_ok := await hass.config_entries.async_unload_platforms(entry, PLATFORMS):
        hass.data[DOMAIN].pop(entry.entry_id)

    return unload_ok


class ChargeCloudDataUpdateCoordinator(DataUpdateCoordinator):
    """My custom coordinator."""

    def __init__(self, hass, api: chargecloudapi.Api, evse_ids: list[EvseId]):
        """Initialize my coordinator."""
        super().__init__(
            hass,
            _LOGGER,
            name=DOMAIN,
            update_interval=UPDATE_INTERVAL,
        )
        self.api = api
        self.evse_ids = evse_ids
        self.smart_call_data: dict[str, chargecloudapi.SmartCallData] = {}

    async def _async_update_data(self) -> dict[str, chargecloudapi.Location]:
        """Fetch the latest location response for every configured EVSE.

        This is the place to pre-process the data to lookup tables
        so entities can quickly look up their data.
        """
        # Note: asyncio.TimeoutError and aiohttp.ClientError are already
        # handled by the data update coordinator.
        async with async_timeout.timeout(10):
            async def fetch_location(
                evse_id: EvseId,
            ) -> tuple[EvseId, chargecloudapi.Location | None]:
                """Fetch one EVSE, preserving its operator lookup data."""
                location, smart_call_data = await self.api.perform_smart_api_call(
                    evse_id, self.smart_call_data.get(evse_id)
                )
                self.smart_call_data[evse_id] = smart_call_data
                if location is None:
                    _LOGGER.info("received empty update for %s", evse_id)
                return evse_id, location

            responses = await asyncio.gather(
                *(fetch_location(evse_id) for evse_id in self.evse_ids)
            )
            return {
                evse_id: location
                for evse_id, location in responses
                if location is not None
            }
