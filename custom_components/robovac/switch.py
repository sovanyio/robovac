from __future__ import annotations
import logging
import json
import base64
from typing import TYPE_CHECKING, Any

from homeassistant.components.switch import SwitchEntity, SwitchDeviceClass
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_NAME, CONF_ID, EntityCategory
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.restore_state import RestoreEntity

from .const import CONF_VACS, DOMAIN
from .vacuums.base import RobovacCommand
if TYPE_CHECKING:
    from .vacuum import RoboVacEntity

_LOGGER = logging.getLogger(__name__)

async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the Eufy RoboVac switch platform."""
    vacuums = config_entry.data[CONF_VACS]

    for item in vacuums:
        item = vacuums[item]

        @callback
        def async_setup_dynamic_switches(config_item: dict[str, Any]) -> None:
            vacuum_entity: RoboVacEntity | None = hass.data[DOMAIN][CONF_VACS].get(item[CONF_ID])
            if not vacuum_entity:
                return

            switches = []
            
            # Station Switches
            if vacuum_entity.get_dps_code("STATION"):
                switches.extend([
                    RobovacStationSwitch(config_item, "Dust Collection", ["dustCollect", "switch"]),
                    RobovacStationSwitch(config_item, "Roller Auto Clean", ["rollAutoClean", "switch"]),
                    RobovacStationSwitch(config_item, "Child Lock", ["childLock", "switch"])
                ])

            # DPS Switches
            if vacuum_entity.get_dps_code(RobovacCommand.AUTO_RETURN):
                switches.append(RobovacDpsSwitch(config_item, "Auto Return Cleaning", RobovacCommand.AUTO_RETURN))
            if vacuum_entity.get_dps_code(RobovacCommand.DO_NOT_DISTURB):
                switches.append(RobovacDpsSwitch(config_item, "Do Not Disturb", RobovacCommand.DO_NOT_DISTURB))
            if vacuum_entity.get_dps_code("activity_log"):
                switches.append(RobovacBase64Switch(config_item, "Activity Log Telemetrics", "activity_log", "switch"))

            if switches:
                async_add_entities(switches)

        async_dispatcher_connect(hass, f"robovac_{item[CONF_ID]}_setup_sensors", async_setup_dynamic_switches)

class RobovacStationSwitch(RestoreEntity, SwitchEntity):
    """Representation of a Eufy RoboVac Station Switch."""

    _attr_has_entity_name = True
    _attr_entity_category = EntityCategory.CONFIG
    _attr_should_poll = True

    def __init__(self, item: dict[str, Any], name: str, data_path: list[str]) -> None:
        """Initialize the switch."""
        self.robovac_id = item[CONF_ID]
        self._data_path = data_path
        
        path_str = "_".join(data_path)
        self._attr_unique_id = f"{item[CONF_ID]}_station_switch_{path_str}"
        self._attr_name = name
        self._attr_is_on = False

        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, item[CONF_ID])},
            name=item[CONF_NAME]
        )

    async def async_added_to_hass(self) -> None:
        """Handle entity which will be added."""
        await super().async_added_to_hass()
        if (last_state := await self.async_get_last_state()) is not None:
            self._attr_is_on = last_state.state == "on"

        # Listen for updates from the vacuum
        self.async_on_remove(
            async_dispatcher_connect(
                self.hass, f"robovac_{self.robovac_id}_updated", self.async_write_ha_state
            )
        )

    @property
    def available(self) -> bool:
        """Return True if entity is available."""
        vacuum_entity: RoboVacEntity | None = self.hass.data[DOMAIN][CONF_VACS].get(self.robovac_id)
        if vacuum_entity:
            return vacuum_entity.has_data_or_connected
        return False

    @property
    def is_on(self) -> bool:
        """Return True if the switch is on."""
        vacuum_entity: RoboVacEntity | None = self.hass.data[DOMAIN][CONF_VACS].get(self.robovac_id)
        if not vacuum_entity or not vacuum_entity.station:
            return self._attr_is_on

        current_data = vacuum_entity.station
        for key in self._data_path:
            if isinstance(current_data, dict) and key in current_data:
                current_data = current_data[key]
            else:
                return self._attr_is_on

        return current_data == "ON"

    async def _async_set_state(self, state: bool) -> None:
        try:
            vacuum_entity: RoboVacEntity | None = self.hass.data[DOMAIN][CONF_VACS].get(self.robovac_id)
            if not vacuum_entity or not vacuum_entity.station or not vacuum_entity.vacuum:
                return

            import copy
            # ⚡ Fix: Use the absolute latest STATION data from vacuum._dps
            station_code = vacuum_entity.get_dps_code("STATION")
            latest_raw = vacuum_entity.vacuum._dps.get(station_code)
            
            if isinstance(latest_raw, str):
                station_data = json.loads(base64.b64decode(latest_raw).decode("utf-8"))
            else:
                station_data = copy.deepcopy(vacuum_entity.station) or {}
            
            # Navigate and set
            current = station_data
            for key in self._data_path[:-1]:
                if key not in current or not isinstance(current[key], dict):
                    current[key] = {}
                current = current[key]
                
            current[self._data_path[-1]] = "ON" if state else "OFF"
            
            json_str = json.dumps(station_data, separators=(",", ":"))
            base64_str = base64.b64encode(json_str.encode("utf8")).decode("utf8")
            
            await vacuum_entity.vacuum.async_set({vacuum_entity.get_dps_code("STATION"): base64_str})
            self._attr_is_on = state
            self.async_write_ha_state()
            
        except Exception as ex:
            _LOGGER.error("Failed to set station switch %s for %s: %s", self._attr_name, self.robovac_id, ex)

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Turn the switch on."""
        await self._async_set_state(True)

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Turn the switch off."""
        await self._async_set_state(False)


class RobovacDpsSwitch(RestoreEntity, SwitchEntity):
    """Representation of a standard boolean DPS switch."""

    _attr_has_entity_name = True
    _attr_entity_category = EntityCategory.CONFIG
    _attr_should_poll = False

    def __init__(self, item: dict[str, Any], name: str, command: str) -> None:
        """Initialize the switch."""
        self.robovac_id = item[CONF_ID]
        self.command = command
        self._attr_unique_id = f"{item[CONF_ID]}_dps_switch_{command}"
        self._attr_name = name
        self._attr_is_on = False

        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, item[CONF_ID])},
            name=item[CONF_NAME]
        )

    async def async_added_to_hass(self) -> None:
        """Handle entity which will be added."""
        await super().async_added_to_hass()
        self.async_on_remove(
            async_dispatcher_connect(
                self.hass, f"robovac_{self.robovac_id}_updated", self.async_write_ha_state
            )
        )

    @property
    def available(self) -> bool:
        """Return True if entity is available."""
        vacuum_entity: RoboVacEntity | None = self.hass.data[DOMAIN][CONF_VACS].get(self.robovac_id)
        if vacuum_entity:
            return vacuum_entity.has_data_or_connected
        return False

    @property
    def is_on(self) -> bool:
        """Return True if switch is on."""
        vacuum_entity: RoboVacEntity | None = self.hass.data[DOMAIN][CONF_VACS].get(self.robovac_id)
        if not vacuum_entity or not vacuum_entity.vacuum:
            return self._attr_is_on
            
        dps_code = vacuum_entity.get_dps_code(self.command)
        if not dps_code:
            return self._attr_is_on
            
        val = vacuum_entity.vacuum._dps.get(dps_code)
        if val is not None:
            self._attr_is_on = bool(val)
            
        return self._attr_is_on

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Turn the switch on."""
        vacuum_entity: RoboVacEntity | None = self.hass.data[DOMAIN][CONF_VACS].get(self.robovac_id)
        if vacuum_entity and vacuum_entity.vacuum:
            dps_code = vacuum_entity.get_dps_code(self.command)
            await vacuum_entity.vacuum.async_set({dps_code: True})
            self._attr_is_on = True
            self.async_write_ha_state()

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Turn the switch off."""
        vacuum_entity: RoboVacEntity | None = self.hass.data[DOMAIN][CONF_VACS].get(self.robovac_id)
        if vacuum_entity and vacuum_entity.vacuum:
            dps_code = vacuum_entity.get_dps_code(self.command)
            await vacuum_entity.vacuum.async_set({dps_code: False})
            self._attr_is_on = False
            self.async_write_ha_state()


class RobovacBase64Switch(RestoreEntity, SwitchEntity):
    """Representation of a Base64 JSON DPS switch."""

    _attr_has_entity_name = True
    _attr_entity_category = EntityCategory.CONFIG
    _attr_should_poll = False

    def __init__(self, item: dict[str, Any], name: str, command: str, json_key: str) -> None:
        """Initialize the switch."""
        self.robovac_id = item[CONF_ID]
        self.command = command
        self.json_key = json_key
        self._attr_unique_id = f"{item[CONF_ID]}_b64_switch_{command}_{json_key}"
        self._attr_name = name
        self._attr_is_on = False

        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, item[CONF_ID])},
            name=item[CONF_NAME]
        )

    async def async_added_to_hass(self) -> None:
        """Handle entity which will be added."""
        await super().async_added_to_hass()
        self.async_on_remove(
            async_dispatcher_connect(
                self.hass, f"robovac_{self.robovac_id}_updated", self.async_write_ha_state
            )
        )

    @property
    def available(self) -> bool:
        """Return True if entity is available."""
        vacuum_entity: RoboVacEntity | None = self.hass.data[DOMAIN][CONF_VACS].get(self.robovac_id)
        if vacuum_entity:
            return vacuum_entity.has_data_or_connected
        return False

    @property
    def is_on(self) -> bool:
        """Return True if switch is on."""
        vacuum_entity: RoboVacEntity | None = self.hass.data[DOMAIN][CONF_VACS].get(self.robovac_id)
        if not vacuum_entity or not vacuum_entity.vacuum:
            return self._attr_is_on
            
        dps_code = vacuum_entity.get_dps_code(self.command)
        if not dps_code:
            return self._attr_is_on
            
        latest_raw = vacuum_entity.vacuum._dps.get(dps_code)
        if isinstance(latest_raw, str):
            try:
                data = json.loads(base64.b64decode(latest_raw).decode("utf-8"))
                val = data.get(self.json_key)
                if val is not None:
                    self._attr_is_on = (val == "ON")
            except Exception:
                pass
            
        return self._attr_is_on

    async def _async_set_state(self, state: bool) -> None:
        try:
            vacuum_entity: RoboVacEntity | None = self.hass.data[DOMAIN][CONF_VACS].get(self.robovac_id)
            if not vacuum_entity or not vacuum_entity.vacuum:
                return

            dps_code = vacuum_entity.get_dps_code(self.command)
            latest_raw = vacuum_entity.vacuum._dps.get(dps_code)
            
            if isinstance(latest_raw, str):
                data = json.loads(base64.b64decode(latest_raw).decode("utf-8"))
            else:
                data = {}
            
            data[self.json_key] = "ON" if state else "OFF"
            
            json_str = json.dumps(data, separators=(",", ":"))
            base64_str = base64.b64encode(json_str.encode("utf8")).decode("utf8")
            
            await vacuum_entity.vacuum.async_set({dps_code: base64_str})
            self._attr_is_on = state
            self.async_write_ha_state()
            
        except Exception as ex:
            _LOGGER.error("Failed to set Base64 switch %s for %s: %s", self._attr_name, self.robovac_id, ex)

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Turn the switch on."""
        await self._async_set_state(True)

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Turn the switch off."""
        await self._async_set_state(False)
