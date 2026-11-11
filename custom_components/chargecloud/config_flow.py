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
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .const import DOMAIN
from .evse_id import is_valid_evse_id
from .grouping import (
    GROUPING_MODE_AUTO,
    GROUPING_MODE_SEPARATE,
    OPTION_GROUPING_MODE,
    OPTION_MERGE_WITH,
    StationMetadata,
    find_device_group_id,
)

_LOGGER = logging.getLogger(__name__)


STEP_USER_DATA_SCHEMA = vol.Schema(
    {
        vol.Required("evse_id"): str,
    }
)


def evse_id(value: str) -> str:
    """Verify evse_id is syntactically correct."""
    if not is_valid_evse_id(value):
        raise vol.Invalid(message="malformed evse-id")
    return value


async def validate_input(hass: HomeAssistant, data: dict[str, Any]) -> dict[str, Any]:
    """Validate the user input allows us to connect.

    Data has the keys from STEP_USER_DATA_SCHEMA with values provided by the user.
    """
    try:
        evse_id(data["evse_id"])
    except vol.Invalid as exc:
        raise MalformedEvseId() from exc

    api = chargecloudapi.Api(websession=async_get_clientsession(hass))
    try:
        location, _ = await api.perform_smart_api_call(data["evse_id"], None)
    except Exception as exc:
        _LOGGER.exception("validate_info failed")
        raise CannotConnect(exc) from exc

    if not location or not location.evses:
        raise EmptyResponse()

    # The API lookup can return several EVSEs for a location. Keep the exact
    # EVSE the user entered so a later port entry remains a distinct entity.
    if not any(evse.id == data["evse_id"] for evse in location.evses):
        raise EmptyResponse()

    metadata = StationMetadata.from_location(location)
    entries = hass.config_entries.async_entries(DOMAIN)
    validated_data = {
        **data,
        **metadata.as_config_data(),
        "device_group_id": find_device_group_id(metadata, data["evse_id"], entries),
    }

    return validated_data


class ConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle a config flow for chargecloud."""

    VERSION = 1

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Handle the initial step."""
        if user_input is None:
            return self.async_show_form(
                step_id="user", data_schema=STEP_USER_DATA_SCHEMA
            )

        errors = {}
        if await self.is_duplicate(user_input):
            return self.async_abort(reason="already_configured")

        try:
            user_input = await validate_input(self.hass, user_input)
        except CannotConnect:
            errors["base"] = "cannot_connect"
        except MalformedEvseId:
            errors["evse_id"] = "malformed_evse_id"
        except EmptyResponse:
            errors["base"] = "empty_response"
        except Exception:  # pylint: disable=broad-except
            _LOGGER.exception("Unexpected exception")
            errors["base"] = "unknown"
        else:
            if await self.is_duplicate(user_input):
                return self.async_abort(reason="already_configured")
            return self.async_create_entry(
                title=user_input.get("location_name") or user_input["evse_id"],
                data=user_input,
            )

        return self.async_show_form(
            step_id="user", data_schema=STEP_USER_DATA_SCHEMA, errors=errors
        )

    async def is_duplicate(self, user_input: dict[str, Any]) -> bool:
        """Check if current device is already configured."""
        for other_evse in self._async_current_entries():
            if other_evse.data.get("evse_id") == user_input["evse_id"]:
                return True
        return False

    @staticmethod
    @callback
    def async_get_options_flow(
        config_entry: config_entries.ConfigEntry,
    ) -> config_entries.OptionsFlow:
        """Return the options flow for manual station grouping."""
        return ChargeCloudOptionsFlow(config_entry)


class ChargeCloudOptionsFlow(config_entries.OptionsFlow):
    """Allow EVSEs to be separated or manually merged into one device."""

    def __init__(self, config_entry: config_entries.ConfigEntry) -> None:
        self.config_entry = config_entry

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Configure automatic grouping and manual merge targets."""
        entries = [
            entry
            for entry in self.hass.config_entries.async_entries(DOMAIN)
            if entry.entry_id != self.config_entry.entry_id
            and entry.data.get("evse_id")
        ]
        merge_options = {
            entry.data["evse_id"]: entry.data["evse_id"] for entry in entries
        }

        if user_input is not None:
            options = {
                OPTION_GROUPING_MODE: user_input[OPTION_GROUPING_MODE],
                OPTION_MERGE_WITH: user_input.get(OPTION_MERGE_WITH, []),
            }
            return self.async_create_entry(title="", data=options)

        current_merge_with = [
            evse_id
            for evse_id in self.config_entry.options.get(OPTION_MERGE_WITH, [])
            if evse_id in merge_options
        ]
        schema: dict[Any, Any] = {
            vol.Required(
                OPTION_GROUPING_MODE,
                default=self.config_entry.options.get(
                    OPTION_GROUPING_MODE, GROUPING_MODE_AUTO
                ),
            ): vol.In([GROUPING_MODE_AUTO, GROUPING_MODE_SEPARATE]),
        }
        if merge_options:
            schema[
                vol.Optional(OPTION_MERGE_WITH, default=current_merge_with)
            ] = cv.multi_select(merge_options)

        data_schema = vol.Schema(schema)
        return self.async_show_form(step_id="init", data_schema=data_schema)


class CannotConnect(HomeAssistantError):
    """Error to indicate we cannot connect."""


class EmptyResponse(HomeAssistantError):
    """Error to indicate we didn't find the evse."""


class MalformedEvseId(HomeAssistantError):
    """Error to indicate a syntactically wrong evse-id."""
