from __future__ import annotations
import logging
from typing import TYPE_CHECKING, Any

from homeassistant.components.binary_sensor import (
    BinarySensorEntity,
    BinarySensorDeviceClass,
)
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
    """Set up the Eufy RoboVac binary sensor platform."""
    vacuums = config_entry.data[CONF_VACS]
    entities = []

    for item in vacuums:
        item = vacuums[item]
        entities.append(RobovacChargingSensor(item))

        @callback
        def async_setup_dynamic_binary_sensors(config_item: dict[str, Any]) -> None:
            vacuum_entity: RoboVacEntity | None = hass.data[DOMAIN][CONF_VACS].get(item[CONF_ID])
            if not vacuum_entity:
                return

            dynamic_entities = []

            # Station Binary Sensors
            if vacuum_entity.get_dps_code("STATION"):
                dynamic_entities.extend([
                    RobovacStationBinarySensor(config_item, "Dust Collecting", ["Collecting"]),
                    RobovacStationBinarySensor(config_item, "Roller Auto Cleaning", ["RollAutoCleaning"])
                ])

            if dynamic_entities:
                async_add_entities(dynamic_entities)

        async_dispatcher_connect(hass, f"robovac_{item[CONF_ID]}_setup_sensors", async_setup_dynamic_binary_sensors)

    async_add_entities(entities)

class RobovacChargingSensor(RestoreEntity, BinarySensorEntity):
    """Binary sensor for vacuum charging state."""

    _attr_has_entity_name = True
    _attr_device_class = BinarySensorDeviceClass.BATTERY_CHARGING
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_should_poll = True

    def __init__(self, item: dict[str, Any]) -> None:
        """Initialize the sensor."""
        self.robovac_id = item[CONF_ID]
        self._attr_unique_id = f"{item[CONF_ID]}_charging"
        self._attr_name = "Charging"
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
        """Return True if the vacuum is charging."""
        vacuum_entity: RoboVacEntity | None = self.hass.data[DOMAIN][CONF_VACS].get(self.robovac_id)
        if vacuum_entity:
            return vacuum_entity.tuya_state == "Charging"
        return self._attr_is_on

class RobovacStationBinarySensor(RestoreEntity, BinarySensorEntity):
    """Binary sensor for station-specific states (Collecting, etc)."""

    _attr_has_entity_name = True
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_should_poll = True

    def __init__(self, item: dict[str, Any], name: str, active_states: list[str]) -> None:
        """Initialize the sensor."""
        self.robovac_id = item[CONF_ID]
        self._active_states = active_states
        self._attr_unique_id = f"{item[CONF_ID]}_{name.lower().replace(' ', '_')}"
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
        """Return True if the vacuum is in one of the active states."""
        vacuum_entity: RoboVacEntity | None = self.hass.data[DOMAIN][CONF_VACS].get(self.robovac_id)
        if vacuum_entity:
            return vacuum_entity.tuya_state in self._active_states
        return self._attr_is_on
