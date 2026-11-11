"""Config flow for chargecloud integration."""
from __future__ import annotations

import logging
from typing import Any

import chargecloudapi
import voluptuous as vol

from homeassistant import config_entries
from homeassistant.core import HomeAssistant, callback
from homeassistant.data_entry_flow import FlowResult
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .const import DOMAIN
from .evse_id import is_valid_evse_id
from .station import (
    OPTION_MERGE_STATIONS,
    OPTION_SPLIT_EVSE_IDS,
    StationMetadata,
    configured_evse_ids,
    find_station_entries,
    station_device_identifier,
)

_LOGGER = logging.getLogger(__name__)

STEP_USER_DATA_SCHEMA = vol.Schema({vol.Required("evse_id"): str})


def evse_id(value: str) -> str:
    """Verify EVSE ID syntax without changing the user-entered ID."""
    if not is_valid_evse_id(value):
        raise vol.Invalid(message="malformed evse-id")
    return value


async def validate_input(hass: HomeAssistant, data: dict[str, Any]) -> dict[str, Any]:
    """Validate the EVSE and return the station metadata from Chargecloud."""
    try:
        evse_id(data["evse_id"])
    except vol.Invalid as exc:
        raise MalformedEvseId() from exc

    api = chargecloudapi.Api(websession=async_get_clientsession(hass))
    try:
        location, _ = await api.perform_smart_api_call(data["evse_id"], None)
    except Exception as exc:
        _LOGGER.exception("EVSE validation failed")
        raise CannotConnect(exc) from exc

    if not location or not location.evses:
        raise EmptyResponse()

    # The exact requested EVSE must be present. Do not replace it with the
    # first EVSE returned for this location.
    if not any(evse.id == data["evse_id"] for evse in location.evses):
        raise EmptyResponse()

    return {**data, **StationMetadata.from_location(location).as_config_data()}


class ConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle a Chargecloud station config flow."""

    VERSION = 2

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Add a station or append an EVSE to an existing station entry."""
        if user_input is None:
            return self.async_show_form(
                step_id="user", data_schema=STEP_USER_DATA_SCHEMA
            )

        if self.is_duplicate(user_input["evse_id"]):
            return self.async_abort(reason="already_configured")

        errors: dict[str, str] = {}
        try:
            validated = await validate_input(self.hass, user_input)
        except CannotConnect:
            errors["base"] = "cannot_connect"
        except MalformedEvseId:
            errors["evse_id"] = "malformed_evse_id"
        except EmptyResponse:
            errors["base"] = "empty_response"
        except Exception:  # pylint: disable=broad-except
            _LOGGER.exception("Unexpected exception during EVSE validation")
            errors["base"] = "unknown"
        else:
            metadata = StationMetadata.from_config_data(validated)
            assert metadata is not None
            matching_entries = find_station_entries(
                metadata, self._async_current_entries()
            )

            if len(matching_entries) == 1:
                await self._async_add_evse_to_station(
                    matching_entries[0], validated, metadata
                )
                return self.async_abort(reason="evse_added")

            if len(matching_entries) > 1:
                return self.async_abort(reason="merge_station_entries")

            station_id = station_device_identifier(
                metadata, validated["evse_id"]
            )
            entry_data = {
                **validated,
                "evse_ids": [validated["evse_id"]],
                "station_id": station_id,
            }
            entry_data.pop("evse_id")
            return self.async_create_entry(
                title=metadata.device_name,
                data=entry_data,
            )

        return self.async_show_form(
            step_id="user", data_schema=STEP_USER_DATA_SCHEMA, errors=errors
        )

    def is_duplicate(self, evse_id: str) -> bool:
        """Check whether this EVSE is already included in a station entry."""
        return any(
            evse_id in configured_evse_ids(entry.data)
            for entry in self._async_current_entries()
        )

    async def _async_add_evse_to_station(
        self,
        entry: config_entries.ConfigEntry,
        validated: dict[str, Any],
        metadata: StationMetadata,
    ) -> None:
        """Append one EVSE to a station config entry and reload it."""
        evse_ids = configured_evse_ids(entry.data)
        evse_id = validated["evse_id"]
        if evse_id not in evse_ids:
            evse_ids.append(evse_id)

        station_metadata = StationMetadata.from_config_data(entry.data) or metadata
        station_id = (
            entry.data.get("station_id")
            or entry.data.get("device_group_id")
            or station_device_identifier(station_metadata, evse_ids[0])
        )
        updated_data = {
            **entry.data,
            **station_metadata.as_config_data(),
            "evse_ids": evse_ids,
            "station_id": station_id,
        }
        updated_data.pop("evse_id", None)
        updated_data.pop("device_group_id", None)
        self.hass.config_entries.async_update_entry(entry, data=updated_data)
        await self.hass.config_entries.async_reload(entry.entry_id)

    @staticmethod
    @callback
    def async_get_options_flow(
        config_entry: config_entries.ConfigEntry,
    ) -> config_entries.OptionsFlow:
        """Return the station options flow."""
        # Home Assistant supplies self.config_entry on the flow instance.
        return ChargeCloudOptionsFlow()


class ChargeCloudOptionsFlow(config_entries.OptionsFlowWithReload):
    """Split ports into separate devices or consolidate legacy entries."""

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Configure port splits and consolidate duplicate station entries."""
        current_evse_ids = configured_evse_ids(self.config_entry.data)
        merge_candidates = [
            entry
            for entry in self.hass.config_entries.async_entries(DOMAIN)
            if entry.entry_id != self.config_entry.entry_id
        ]
        merge_options = {
            entry.entry_id: (
                f"{entry.title} ({', '.join(configured_evse_ids(entry.data))})"
            )
            for entry in merge_candidates
        }

        if user_input is not None:
            split_evse_ids = list(user_input.get(OPTION_SPLIT_EVSE_IDS, []))
            if selected_entries := user_input.get(OPTION_MERGE_STATIONS, []):
                split_evse_ids.extend(
                    await self._async_merge_station_entries(selected_entries)
                )
            return self.async_create_entry(
                title="",
                data={
                    OPTION_SPLIT_EVSE_IDS: [
                        evse_id
                        for evse_id in dict.fromkeys(split_evse_ids)
                        if evse_id in current_evse_ids
                        or evse_id
                        in configured_evse_ids(self.config_entry.data)
                    ]
                },
            )

        schema: dict[Any, Any] = {
            vol.Optional(
                OPTION_SPLIT_EVSE_IDS,
                default=[
                    evse_id
                    for evse_id in current_evse_ids
                    if evse_id
                    in self.config_entry.options.get(OPTION_SPLIT_EVSE_IDS, [])
                ],
            ): cv.multi_select({evse_id: evse_id for evse_id in current_evse_ids}),
        }
        if merge_options:
            schema[vol.Optional(OPTION_MERGE_STATIONS, default=[])] = cv.multi_select(
                merge_options
            )

        return self.async_show_form(
            step_id="init",
            data_schema=self.add_suggested_values_to_schema(
                vol.Schema(schema), self.config_entry.options
            ),
        )

    async def _async_merge_station_entries(self, entry_ids: list[str]) -> list[str]:
        """Move legacy EVSEs and entity registry records into this entry."""
        metadata = StationMetadata.from_config_data(self.config_entry.data)
        if metadata is None:
            return []

        other_entries = [
            entry
            for entry in self.hass.config_entries.async_entries(DOMAIN)
            if entry.entry_id != self.config_entry.entry_id
        ]
        entries = [
            entry for entry in other_entries if entry.entry_id in entry_ids
        ]
        if not entries:
            return []

        evse_ids = configured_evse_ids(self.config_entry.data)
        split_evse_ids: list[str] = []
        for entry in entries:
            for evse_id in configured_evse_ids(entry.data):
                if evse_id not in evse_ids:
                    evse_ids.append(evse_id)
            split_evse_ids.extend(
                entry.options.get(OPTION_SPLIT_EVSE_IDS, [])
            )

        station_id = (
            self.config_entry.data.get("station_id")
            or self.config_entry.data.get("device_group_id")
            or station_device_identifier(metadata, evse_ids[0])
        )
        updated_data = {
            **self.config_entry.data,
            **metadata.as_config_data(),
            "evse_ids": evse_ids,
            "station_id": station_id,
        }
        updated_data.pop("evse_id", None)
        updated_data.pop("device_group_id", None)
        self.hass.config_entries.async_update_entry(
            self.config_entry, data=updated_data
        )

        entity_registry = er.async_get(self.hass)
        device_registry = dr.async_get(self.hass)
        for entry in entries:
            entity_entries = er.async_entries_for_config_entry(
                entity_registry, entry.entry_id
            )
            old_device_ids = {
                entity.device_id for entity in entity_entries if entity.device_id
            }
            old_device_ids.update(
                device.id
                for device in dr.async_entries_for_config_entry(
                    device_registry, entry.entry_id
                )
            )
            for entity in entity_entries:
                entity_registry.async_update_entity(
                    entity.entity_id,
                    config_entry_id=self.config_entry.entry_id,
                    config_subentry_id=None,
                    device_id=None,
                )
            for device_id in old_device_ids:
                if not er.async_entries_for_device(entity_registry, device_id):
                    device = device_registry.async_get(device_id)
                    if device is not None:
                        try:
                            device_registry.async_remove_device(device_id)
                        except ValueError:
                            _LOGGER.debug(
                                "Could not remove old device %s while merging entries",
                                device_id,
                                exc_info=True,
                            )
            await self.hass.config_entries.async_remove(entry.entry_id)
        return split_evse_ids


class CannotConnect(HomeAssistantError):
    """Error to indicate we cannot connect."""


class EmptyResponse(HomeAssistantError):
    """Error to indicate the EVSE was not returned by Chargecloud."""


class MalformedEvseId(HomeAssistantError):
    """Error to indicate a syntactically wrong EVSE ID."""
