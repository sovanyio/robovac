# Copyright 2022 Brendan McCluskey
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#    https://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Config flow for Eufy Robovac integration."""

from __future__ import annotations

import json
import logging
from copy import deepcopy
from typing import Any, Optional

import requests
import voluptuous as vol

import homeassistant.helpers.config_validation as cv
from homeassistant import config_entries
from homeassistant.config_entries import ConfigFlowResult
from homeassistant.const import (
    CONF_ACCESS_TOKEN,
    CONF_CLIENT_ID,
    CONF_COUNTRY_CODE,
    CONF_DESCRIPTION,
    CONF_ID,
    CONF_IP_ADDRESS,
    CONF_MAC,
    CONF_MODEL,
    CONF_NAME,
    CONF_PASSWORD,
    CONF_REGION,
    CONF_TIME_ZONE,
    CONF_USERNAME,
)
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import selector

from .const import CONF_AUTODISCOVERY, CONF_VACS, DOMAIN
from .countries import (
    get_phone_code_by_country_code,
    get_phone_code_by_region,
    get_region_by_country_code,
    get_region_by_phone_code,
)
from .eufywebapi import EufyLogon
from .tuyawebapi import TuyaAPISession

_LOGGER = logging.getLogger(__name__)

USER_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_USERNAME): cv.string,
        vol.Required(CONF_PASSWORD): cv.string,
    }
)


def get_eufy_vacuums(self: dict[str, Any]) -> requests.Response:
    """Login to Eufy and get the vacuum details.

    Returns:
        Response: The API response containing vacuum information

    Raises:
        CannotConnect: If connection to the API fails
        InvalidAuth: If authentication fails
    """
    eufy_session = EufyLogon(self["username"], self["password"])
    response = eufy_session.get_user_info()

    # Check if response is valid
    if response is None:
        raise CannotConnect
    if response.status_code != 200:
        raise CannotConnect

    user_response = response.json()
    if user_response["res_code"] != 1:
        raise InvalidAuth

    response = eufy_session.get_device_info(
        user_response["user_info"]["request_host"],
        user_response["user_info"]["id"],
        user_response["access_token"],
    )

    # Check if response is valid
    if response is None:
        raise CannotConnect

    device_response = response.json()

    response = eufy_session.get_user_settings(
        user_response["user_info"]["request_host"],
        user_response["user_info"]["id"],
        user_response["access_token"],
    )

    # Check if response is valid
    if response is None:
        raise CannotConnect

    settings_response = response.json()

    self[CONF_CLIENT_ID] = user_response["user_info"]["id"]
    if (
        "tuya_home" in settings_response["setting"]["home_setting"]
        and "tuya_region_code"
        in settings_response["setting"]["home_setting"]["tuya_home"]
    ):
        self[CONF_REGION] = settings_response["setting"]["home_setting"]["tuya_home"][
            "tuya_region_code"
        ]
        if user_response["user_info"]["phone_code"]:
            self[CONF_COUNTRY_CODE] = user_response["user_info"]["phone_code"]
        else:
            self[CONF_COUNTRY_CODE] = get_phone_code_by_region(self[CONF_REGION])
    elif user_response["user_info"]["phone_code"]:
        self[CONF_REGION] = get_region_by_phone_code(
            user_response["user_info"]["phone_code"]
        )
        self[CONF_COUNTRY_CODE] = user_response["user_info"]["phone_code"]
    elif user_response["user_info"]["country"]:
        self[CONF_REGION] = get_region_by_country_code(
            user_response["user_info"]["country"]
        )
        self[CONF_COUNTRY_CODE] = get_phone_code_by_country_code(
            user_response["user_info"]["country"]
        )
    else:
        self[CONF_REGION] = "EU"
        self[CONF_COUNTRY_CODE] = "44"

    self[CONF_TIME_ZONE] = user_response["user_info"]["timezone"]

    tuya_client = TuyaAPISession(
        username="eh-" + self[CONF_CLIENT_ID],
        region=self[CONF_REGION],
        timezone=self[CONF_TIME_ZONE],
        phone_code=self[CONF_COUNTRY_CODE],
    )

    items = device_response["devices"]
    self[CONF_VACS] = {}
    for item in items:
        if item["product"]["appliance"] == "Cleaning":
            try:
                device = tuya_client.get_device(item["id"])

                vac_details = {
                    CONF_ID: item["id"],
                    CONF_MODEL: item["product"]["product_code"],
                    CONF_NAME: item["alias_name"],
                    CONF_DESCRIPTION: item["name"],
                    CONF_MAC: item["wifi"]["mac"],
                    CONF_IP_ADDRESS: "",
                    CONF_AUTODISCOVERY: True,
                    CONF_ACCESS_TOKEN: device["localKey"],
                }
                self[CONF_VACS][item["id"]] = vac_details
            except Exception:
                _LOGGER.debug(
                    "Skipping vacuum {}: found on Eufy but not on Tuya. Eufy details:".format(
                        item["id"]
                    )
                )
                _LOGGER.debug(json.dumps(item, indent=2))

    # Ensure we're returning a valid Response object as declared in the return type
    if response is None:
        raise CannotConnect
    return response


async def validate_input(hass: HomeAssistant, data: dict[str, Any]) -> dict[str, Any]:
    """Validate the user input allows us to connect."""
    await hass.async_add_executor_job(get_eufy_vacuums, data)
    return data


class ConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):  # type: ignore[call-arg]
    """Handle a config flow for Eufy Robovac."""

    data: Optional[dict[str, Any]]

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Handle the initial step."""
        if user_input is None:
            # Return form for user input
            return self.async_show_form(
                step_id="user", data_schema=USER_SCHEMA
            )  # type: ignore[return-value]
        errors = {}
        try:
            unique_id = user_input[CONF_USERNAME]
            valid_data = await validate_input(self.hass, user_input)
        except CannotConnect:
            errors["base"] = "cannot_connect"
        except InvalidAuth:
            errors["base"] = "invalid_auth"
        except Exception as e:  # pylint: disable=broad-except
            _LOGGER.exception("Unexpected exception: {}".format(e))
            errors["base"] = "unknown"
        else:
            await self.async_set_unique_id(unique_id)
            self._abort_if_unique_id_configured()
            # return await self.async_step_repo(valid_data)
            # Create the config entry with validated data
            return self.async_create_entry(
                title=unique_id, data=valid_data
            )  # type: ignore[return-value]
        return self.async_show_form(
            step_id="user", data_schema=USER_SCHEMA, errors=errors
        )  # type: ignore[return-value]

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: config_entries.ConfigEntry) -> OptionsFlowHandler:
        """Get the options flow for this handler."""
        return OptionsFlowHandler(config_entry)


class CannotConnect(HomeAssistantError):
    """Error to indicate we cannot connect."""


class InvalidAuth(HomeAssistantError):
    """Error to indicate there is invalid auth."""


class OptionsFlowHandler(config_entries.OptionsFlow):
    """Handles options flow for the component."""

    def __init__(self, config_entry: config_entries.ConfigEntry) -> None:
        self._config_entry = config_entry
        self.selected_vacuum = None
        self.selected_map_id = None
        self.selected_room_id = None
        self._vacuums = deepcopy(config_entry.data.get(CONF_VACS, {}))

    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Handle the initial step."""
        errors: dict[str, str] = {}

        if user_input is not None:
            self.selected_vacuum = user_input["selected_vacuum"]
            return await self.async_step_edit()

        vacuums_config = self.config_entry.data[CONF_VACS]
        vacuum_list = {}
        for vacuum_id in vacuums_config:
            vacuum_list[vacuum_id] = vacuums_config[vacuum_id]["name"]

        devices_schema = vol.Schema(
            {vol.Required("selected_vacuum"): vol.In(vacuum_list)}
        )

        return self.async_show_form(
            step_id="init", data_schema=devices_schema, errors=errors
        )  # type: ignore[return-value]

    async def async_step_edit(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Handle the edit step."""
        errors: dict[str, str] = {}
        vacuum = self._vacuums[self.selected_vacuum]

        if user_input is not None:
            # Handle Navigation
            action = user_input.get("action")
            
            # Save basic settings
            vacuum[CONF_AUTODISCOVERY] = user_input[CONF_AUTODISCOVERY]
            if user_input.get(CONF_IP_ADDRESS):
                vacuum[CONF_IP_ADDRESS] = user_input[CONF_IP_ADDRESS]

            if action == "manage_maps":
                return await self.async_step_manage_maps()
            
            # Final Save
            self.hass.config_entries.async_update_entry(
                self.config_entry,
                data={CONF_VACS: self._vacuums},
            )
            return self.async_create_entry(title="", data={})

        options_schema = vol.Schema({
            vol.Required(CONF_AUTODISCOVERY, default=vacuum.get(CONF_AUTODISCOVERY, True)): bool,
            vol.Optional(CONF_IP_ADDRESS, default=vacuum.get(CONF_IP_ADDRESS)): str,
            vol.Required("action", default="save"): vol.In({
                "save": "Save and Exit",
                "manage_maps": "Manage Map & Room ID Mappings"
            })
        })

        return self.async_show_form(
            step_id="edit", data_schema=options_schema, errors=errors
        )

    async def async_step_manage_maps(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Step to list and manage maps."""
        vacuum = self._vacuums[self.selected_vacuum]
        maps = vacuum.get("structured_maps", {})

        if user_input is not None:
            action = user_input.get("action")
            if action == "add":
                self.selected_map_id = None
                return await self.async_step_edit_map()
            elif action == "back":
                return await self.async_step_edit()
            elif action.startswith("edit_"):
                self.selected_map_id = action.split("edit_")[1]
                return await self.async_step_edit_map()
            elif action.startswith("delete_"):
                map_id = action.split("delete_")[1]
                if map_id in maps:
                    del maps[map_id]
                return await self.async_step_manage_maps()

        # Build map list for selection
        map_options = {"add": "Add New Map", "back": "<< Back to Basic Settings"}
        for mid, mdata in maps.items():
            map_options[f"edit_{mid}"] = f"Edit Map: {mdata['name']} (ID: {mid})"
            map_options[f"delete_{mid}"] = f"Delete Map: {mdata['name']}"

        return self.async_show_form(
            step_id="manage_maps",
            data_schema=vol.Schema({
                vol.Required("action"): vol.In(map_options)
            })
        )

    async def async_step_edit_map(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Step to edit a specific map's details."""
        vacuum = self._vacuums[self.selected_vacuum]
        maps = vacuum.setdefault("structured_maps", {})
        
        current_map = maps.get(self.selected_map_id, {"name": "", "rooms": {}}) if self.selected_map_id else {"name": "", "rooms": {}}

        if user_input is not None:
            map_id = user_input["map_id"]
            map_name = user_input["map_name"]
            
            # If we renamed the ID, move the data
            if self.selected_map_id and self.selected_map_id != map_id:
                maps[map_id] = maps.pop(self.selected_map_id)
            
            maps[map_id] = {
                "name": map_name,
                "rooms": maps.get(map_id, {}).get("rooms", {})
            }
            self.selected_map_id = map_id
            
            if user_input.get("manage_rooms"):
                return await self.async_step_manage_rooms()
            
            return await self.async_step_manage_maps()

        schema = vol.Schema({
            vol.Required("map_id", default=self.selected_map_id or ""): str,
            vol.Required("map_name", default=current_map["name"]): str,
            vol.Optional("manage_rooms", default=False): bool,
        })

        return self.async_show_form(step_id="edit_map", data_schema=schema)

    async def async_step_manage_rooms(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Step to list and manage rooms for a specific map."""
        vacuum = self._vacuums[self.selected_vacuum]
        mdata = vacuum["structured_maps"][self.selected_map_id]
        rooms = mdata.setdefault("rooms", {})

        if user_input is not None:
            action = user_input.get("action")
            if action == "add":
                self.selected_room_id = None
                return await self.async_step_edit_room()
            elif action == "back":
                return await self.async_step_edit_map()
            elif action.startswith("edit_"):
                self.selected_room_id = action.split("edit_")[1]
                return await self.async_step_edit_room()
            elif action.startswith("delete_"):
                room_id = action.split("delete_")[1]
                if room_id in rooms:
                    del rooms[room_id]
                return await self.async_step_manage_rooms()

        room_options = {"add": "Add New Room", "back": f"<< Back to Map: {mdata['name']}"}
        for rid, rname in rooms.items():
            room_options[f"edit_{rid}"] = f"Edit Room: {rname} (ID: {rid})"
            room_options[f"delete_{rid}"] = f"Delete Room: {rname}"

        return self.async_show_form(
            step_id="manage_rooms",
            data_schema=vol.Schema({
                vol.Required("action"): vol.In(room_options)
            })
        )

    async def async_step_edit_room(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Step to edit a specific room's details."""
        vacuum = self._vacuums[self.selected_vacuum]
        mdata = vacuum["structured_maps"][self.selected_map_id]
        rooms = mdata["rooms"]

        if user_input is not None:
            room_id = user_input["room_id"]
            room_name = user_input["room_name"]
            
            if self.selected_room_id and self.selected_room_id != room_id:
                rooms.pop(self.selected_room_id)
            
            rooms[room_id] = room_name
            return await self.async_step_manage_rooms()

        schema = vol.Schema({
            vol.Required("room_id", default=self.selected_room_id or ""): str,
            vol.Required("room_name", default=rooms.get(self.selected_room_id, "")): str,
        })

        return self.async_show_form(step_id="edit_room", data_schema=schema)
