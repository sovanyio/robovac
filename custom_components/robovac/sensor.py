from __future__ import annotations
import logging
from datetime import timedelta
from typing import TYPE_CHECKING, Any

from homeassistant.components.sensor import SensorDeviceClass, SensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import PERCENTAGE, EntityCategory, CONF_NAME, CONF_ID
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
    """Set up the Eufy RoboVac sensor platform."""
    vacuums = config_entry.data[CONF_VACS]
    entities = []

    for item in vacuums:
        item = vacuums[item]
        entities.append(RobovacBatterySensor(item))

        @callback
        def async_setup_dynamic_sensors(config_item: dict[str, Any]) -> None:
            vacuum_entity: RoboVacEntity | None = hass.data[DOMAIN][CONF_VACS].get(item[CONF_ID])
            if not vacuum_entity:
                return

            dynamic_entities = []
            
            # Station Sensors
            if vacuum_entity.get_dps_code("STATION"):
                dynamic_entities.extend([
                    RobovacStationSensor(config_item, "Dustbin Auto Empty", ["dustCollect", "state"]),
                    RobovacStationSensor(config_item, "Dustbin Collection Mode", ["dustCollect", "mode"]),
                    RobovacStationSensor(config_item, "Dustbin Cleaning Reminder", ["dustCollect", "full"]),
                    RobovacStationSensor(config_item, "Roller Auto Clean", ["rollAutoClean", "state"])
                ])

            # Consumable Sensors
            if vacuum_entity.get_dps_code("CONSUMABLES"):
                dynamic_entities.extend([
                    RobovacConsumableSensor(config_item, "Dust Bag", "DB", 50),
                    RobovacConsumableSensor(config_item, "Side Brush", "SB", 180),
                    RobovacConsumableSensor(config_item, "Filter", "FM", 200),
                    RobovacConsumableSensor(config_item, "Rolling Brush", "RB", 360),
                    RobovacConsumableSensor(config_item, "Sensors", "SS", 30),
                    RobovacConsumableSensor(config_item, "Mop Pad", "SP", 150),
                ])
                
            if dynamic_entities:
                async_add_entities(dynamic_entities)

        async_dispatcher_connect(hass, f"robovac_{item[CONF_ID]}_setup_sensors", async_setup_dynamic_sensors)

    async_add_entities(entities)


class RobovacBatterySensor(RestoreEntity, SensorEntity):
    """Representation of a Eufy RoboVac Battery Sensor."""

    _attr_has_entity_name = True
    _attr_device_class = SensorDeviceClass.BATTERY
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_native_unit_of_measurement = PERCENTAGE
    _attr_should_poll = True

    def __init__(self, item: dict[str, Any]) -> None:
        """Initialize the sensor."""
        self.robovac_id = item[CONF_ID]
        self._attr_unique_id = f"{item[CONF_ID]}_battery"
        self._attr_name = "Battery"
        self._attr_native_value = None

        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, item[CONF_ID])},
            name=item[CONF_NAME]
        )

    async def async_added_to_hass(self) -> None:
        """Handle entity which will be added."""
        await super().async_added_to_hass()
        if (last_state := await self.async_get_last_state()) is not None:
            try:
                self._attr_native_value = int(last_state.state)
            except (ValueError, TypeError):
                self._attr_native_value = None

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
    def native_value(self) -> Any:
        """Return the battery level."""
        vacuum_entity: RoboVacEntity | None = self.hass.data[DOMAIN][CONF_VACS].get(self.robovac_id)
        if vacuum_entity and vacuum_entity.battery_percent is not None:
            return vacuum_entity.battery_percent
        return self._attr_native_value

class RobovacStationSensor(RestoreEntity, SensorEntity):
    """Representation of a Eufy RoboVac Station Sensor."""

    _attr_has_entity_name = True
    _attr_should_poll = True

    def __init__(self, item: dict[str, Any], name: str, data_path: list[str]) -> None:
        """Initialize the sensor."""
        self.robovac_id = item[CONF_ID]
        self._data_path = data_path
        
        path_str = "_".join(data_path)
        self._attr_unique_id = f"{item[CONF_ID]}_station_sensor_{path_str}"
        self._attr_name = name
        self._attr_native_value = None

        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, item[CONF_ID])},
            name=item[CONF_NAME]
        )

    async def async_added_to_hass(self) -> None:
        """Handle entity which will be added."""
        await super().async_added_to_hass()
        if (last_state := await self.async_get_last_state()) is not None:
            self._attr_native_value = last_state.state

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
    def native_value(self) -> Any:
        """Return the station sensor value."""
        vacuum_entity: RoboVacEntity | None = self.hass.data[DOMAIN][CONF_VACS].get(self.robovac_id)
        if not vacuum_entity or not vacuum_entity.station:
            return self._attr_native_value

        current_data = vacuum_entity.station
        for key in self._data_path:
            if isinstance(current_data, dict) and key in current_data:
                current_data = current_data[key]
            else:
                return self._attr_native_value

        # Translate values
        if self._data_path == ["dustCollect", "mode"]:
            if current_data == 1:
                freq = vacuum_entity.station.get("dustCollect", {}).get("freq", 1)
                current_data = f"After {freq} clean{'s' if freq > 1 else ''}"
            elif current_data == 2:
                time_val = vacuum_entity.station.get("dustCollect", {}).get("time", 15)
                current_data = f"Every {time_val} mins"
        
        if self._data_path == ["dustCollect", "full"]:
            current_data = f"{current_data} hours"

        if self._data_path[-1] == "state":
            state_map = {
                "I": "Idle",
                "R": "Running",
                "C": "Completed",
                "E": "Emptying"
            }
            current_data = state_map.get(str(current_data), current_data)

        return current_data


class RobovacConsumableSensor(RestoreEntity, SensorEntity):
    """Representation of a Eufy RoboVac Consumable Sensor."""

    _attr_has_entity_name = True
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_native_unit_of_measurement = PERCENTAGE
    _attr_should_poll = False

    def __init__(self, item: dict[str, Any], name: str, key: str, max_hours: int) -> None:
        """Initialize the sensor."""
        self.robovac_id = item[CONF_ID]
        self._key = key
        self._max_hours = max_hours
        self._attr_unique_id = f"{item[CONF_ID]}_consumable_{key}"
        self._attr_name = name
        self._attr_native_value = None

        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, item[CONF_ID])},
            name=item[CONF_NAME]
        )

    async def async_added_to_hass(self) -> None:
        """Handle entity which will be added."""
        await super().async_added_to_hass()
        if (last_state := await self.async_get_last_state()) is not None:
            try:
                self._attr_native_value = int(last_state.state)
            except (ValueError, TypeError):
                self._attr_native_value = None

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
    def native_value(self) -> Any:
        """Return the remaining percentage of the consumable."""
        import base64
        import json
        
        vacuum_entity: RoboVacEntity | None = self.hass.data[DOMAIN][CONF_VACS].get(self.robovac_id)
        if not vacuum_entity or not vacuum_entity.vacuum:
            return self._attr_native_value

        dps_code = vacuum_entity.get_dps_code("consumables")
        if not dps_code:
            return self._attr_native_value

        val = vacuum_entity.vacuum._dps.get(dps_code)
        if val and isinstance(val, str):
            try:
                padded_val = val + "=" * (-len(val) % 4)
                json_str = base64.b64decode(padded_val).decode('utf-8')
                data = json.loads(json_str)
                
                hours_used = data.get("consumable", {}).get("duration", {}).get(self._key)
                if hours_used is not None:
                    # Calculate remaining percentage
                    remaining = max(0, self._max_hours - hours_used)
                    self._attr_native_value = round((remaining / self._max_hours) * 100)
            except Exception:
                pass

        return self._attr_native_value
