"""Platform for sensor integration."""
from __future__ import annotations

import typing

import chargecloudapi
from chargecloudapi import Location

from homeassistant.components.sensor import SensorEntity, SensorDeviceClass
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from . import ChargeCloudDataUpdateCoordinator
from .const import DOMAIN, EvseId
from .station import OPTION_SPLIT_EVSE_IDS, StationMetadata, configured_evse_ids


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    """Set up a Geocaching sensor entry."""
    coordinator = hass.data[DOMAIN][entry.entry_id]
    evse_ids = configured_evse_ids(entry.data)
    metadata = StationMetadata.from_config_data(entry.data)
    device_name = (
        metadata.device_name
        if metadata is not None
        else entry.title
    )
    station_id = entry.data.get("station_id") or f"station:{entry.entry_id}"
    split_evse_ids = set(entry.options.get(OPTION_SPLIT_EVSE_IDS, []))
    async_add_entities(
        [
            ChargeCloudRealtimeSensor(
                evse_id=evse_id,
                coordinator=coordinator,
                station_id=station_id,
                device_name=device_name,
                split=evse_id in split_evse_ids,
            )
            for evse_id in evse_ids
        ]
    )


class ChargeCloudRealtimeSensor(
    CoordinatorEntity[ChargeCloudDataUpdateCoordinator], SensorEntity
):
    """Sensor for one EVSE's realtime availability status."""

    _attr_has_entity_name = True
    _attr_name = None

    def __init__(
        self,
        evse_id: EvseId,
        coordinator: ChargeCloudDataUpdateCoordinator,
        station_id: str,
        device_name: str,
        split: bool,
    ) -> None:
        """Initialize the Sensor."""
        super().__init__(coordinator)
        self.evse_id = evse_id
        self.coordinator = coordinator
        self._attr_unique_id = f"{evse_id}-realtime"
        self._attr_name = evse_id
        self._attr_attribution = "chargecloud.de"
        self._attr_device_class = SensorDeviceClass.ENUM
        self._attr_native_unit_of_measurement = None
        self._attr_state_class = None
        device_identifier = (
            f"{station_id}:evse:{evse_id}" if split else station_id
        )
        self._attr_device_info = DeviceInfo(
            name=f"{device_name} - {evse_id}" if split else device_name,
            identifiers={(DOMAIN, device_identifier)},
            entry_type=None,
        )
        self._attr_options = list(typing.get_args(chargecloudapi.Status))
        # read data from coordinator, which should have data by now
        self._read_coordinator_data()

    @property
    def available(self) -> bool:
        """Only make the sensor available when its EVSE lookup succeeded."""
        return super().available and self.evse_id in (self.coordinator.data or {})

    def _get_location(self) -> Location | None:
        return (self.coordinator.data or {}).get(self.evse_id)

    def _get_evse(self) -> chargecloudapi.Evse | None:
        location = self._get_location()
        if location is None:
            return None
        for evse in location.evses:
            if evse.id == self.evse_id:
                return evse
        return None

    def _choose_icon(self, connectors: list[chargecloudapi.Connector]):
        iconmap: dict[str, str] = {
            "IEC_62196_T2": "mdi:ev-plug-type2",
            "IEC_62196_T2_COMBO": "mdi:ev-plug-ccs2",
            "CHADEMO": "mdi:ev-plug-chademo",
            "TESLA": "mdi:ev-plug-tesla",
            "DOMESTIC_F": "mdi:power-socket-eu",
        }
        if len(connectors) != 1:
            return "mdi:ev-station"
        return iconmap.get(connectors[0].standard, "mdi:ev-station")

    def _read_coordinator_data(self) -> None:
        location = self._get_location()
        evse = self._get_evse()
        if location is None or evse is None:
            self._attr_native_value = None
            return
        self._attr_native_value = evse.status
        self._attr_icon = self._choose_icon(evse.connectors)
        extra_data = {
            "address": location.address,
            "city": location.city,
            "postal_code": location.postal_code,
            "country": location.country,
            "latitude": location.coordinates.latitude,
            "longitude": location.coordinates.longitude,
            "update_opid": self.coordinator.smart_call_data[
                self.evse_id
            ].operator_id.name
            if self.coordinator.smart_call_data.get(self.evse_id)
            else None,
            "connectors": [
                {
                    "power_type": connector.power_type,
                    "ampere": connector.ampere,
                    "voltage": connector.voltage,
                    "max_power": connector.max_power,
                    "standard": connector.standard,
                    "format": connector.format,
                }
                for connector in evse.connectors
            ],
        }
        self._attr_extra_state_attributes = extra_data

    @callback
    def _handle_coordinator_update(self) -> None:
        """Handle updated data from the coordinator."""
        self._read_coordinator_data()
        self.async_write_ha_state()
