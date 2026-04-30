from __future__ import annotations
import logging
import json
import base64
from typing import TYPE_CHECKING, Any

from homeassistant.components.select import SelectEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_NAME, CONF_ID, EntityCategory
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.restore_state import RestoreEntity

from .const import CONF_VACS, DOMAIN

if TYPE_CHECKING:
    from .vacuum import RoboVacEntity

_LOGGER = logging.getLogger(__name__)

async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the Eufy RoboVac select platform."""
    vacuums = config_entry.data[CONF_VACS]

    for item in vacuums:
        item = vacuums[item]

        @callback
        def async_setup_dynamic_selects(config_item: dict[str, Any]) -> None:
            vacuum_entity: RoboVacEntity | None = hass.data[DOMAIN][CONF_VACS].get(item[CONF_ID])
            if not vacuum_entity:
                return

            dynamic_entities = []

            # Station Selects
            if vacuum_entity.get_dps_code("STATION"):
                dynamic_entities.extend([
                    RobovacStationSelect(
                        config_item, 
                        "Dustbin Collection Mode", 
                        ["dustCollect", "mode"],
                        {
                            "1": "After 1 Clean",
                            "2": "After 2 Cleans",
                            "3": "After 3 Cleans",
                            "15": "15 mins (Pet mode)",
                            "30": "30 mins",
                            "45": "45 mins",
                            "60": "60 mins"
                        }
                    )
                ])

            if dynamic_entities:
                async_add_entities(dynamic_entities)

        async_dispatcher_connect(hass, f"robovac_{item[CONF_ID]}_setup_sensors", async_setup_dynamic_selects)

class RobovacStationSelect(RestoreEntity, SelectEntity):
    """Representation of a Eufy RoboVac Station Select entity."""

    _attr_has_entity_name = True
    _attr_entity_category = EntityCategory.CONFIG
    _attr_should_poll = True

    def __init__(self, item: dict[str, Any], name: str, data_path: list[str], options_map: dict[str, str]) -> None:
        """Initialize the select entity."""
        self.robovac_id = item[CONF_ID]
        self._data_path = data_path
        self._options_map = options_map
        self._reverse_options_map = {v: k for k, v in options_map.items()}
        
        path_str = "_".join(data_path)
        self._attr_unique_id = f"{item[CONF_ID]}_station_select_{path_str}"
        self._attr_name = name
        self._attr_options = list(options_map.values())
        self._attr_current_option = None

        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, item[CONF_ID])},
            name=item[CONF_NAME]
        )

    async def async_added_to_hass(self) -> None:
        """Handle entity which will be added."""
        await super().async_added_to_hass()
        if (last_state := await self.async_get_last_state()) is not None:
            self._attr_current_option = last_state.state

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
    def current_option(self) -> str | None:
        """Return the current selected option."""
        vacuum_entity: RoboVacEntity | None = self.hass.data[DOMAIN][CONF_VACS].get(self.robovac_id)
        if not vacuum_entity or not vacuum_entity.station:
            return self._attr_current_option

        current_data = vacuum_entity.station
        for key in self._data_path:
            if isinstance(current_data, dict) and str(key) in current_data:
                current_data = current_data[str(key)]
            elif isinstance(current_data, dict) and key in current_data:
                current_data = current_data[key]
            else:
                return self._attr_current_option

        # Apply specialized logic for Dust Collection Mode
        if self._data_path == ["dustCollect", "mode"]:
            dust_collect = vacuum_entity.station.get("dustCollect", {})
            mode = dust_collect.get("mode")
            if mode == 1:
                val = str(dust_collect.get("freq", 1))
            elif mode == 2:
                val = str(dust_collect.get("time", 15))
            else:
                val = str(mode)
        else:
            val = str(current_data)

        return self._options_map.get(val, self._attr_current_option)

    async def async_select_option(self, option: str) -> None:
        """Change the selected option."""
        try:
            vacuum_entity: RoboVacEntity | None = self.hass.data[DOMAIN][CONF_VACS].get(self.robovac_id)
            if not vacuum_entity or not vacuum_entity.station or not vacuum_entity.vacuum:
                return

            value = self._reverse_options_map.get(option)
            if value is None:
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
                
            # Mode 2 is time-based, Mode 1 is clean-based
            if self._data_path == ["dustCollect", "mode"]:
                if value in ["1", "2", "3"]:
                    current["mode"] = 1
                    current["freq"] = int(value)
                else:
                    current["mode"] = 2
                    current["time"] = int(value)
            else:
                current[self._data_path[-1]] = int(value) if value.isdigit() else value
            
            json_str = json.dumps(station_data, separators=(",", ":"))
            base64_str = base64.b64encode(json_str.encode("utf8")).decode("utf8")
            
            await vacuum_entity.vacuum.async_set({vacuum_entity.get_dps_code("STATION"): base64_str})
            self._attr_current_option = option
            self.async_write_ha_state()
            
        except Exception as ex:
            _LOGGER.error("Failed to set station select %s for %s: %s", self._attr_name, self.robovac_id, ex)
