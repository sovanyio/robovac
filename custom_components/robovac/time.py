from __future__ import annotations
import logging
import base64
from datetime import time
from typing import TYPE_CHECKING, Any

from homeassistant.components.time import TimeEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_NAME, CONF_ID, EntityCategory
from homeassistant.core import HomeAssistant
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
    """Set up the Eufy RoboVac time platform."""
    vacuums = config_entry.data[CONF_VACS]
    entities = []

    for item in vacuums:
        item = vacuums[item]
        # Only add DND schedule if the vacuum supports it
        vacuum_entity: RoboVacEntity | None = hass.data[DOMAIN][CONF_VACS].get(item[CONF_ID])
        
        # We assume if DO_NOT_DISTURB_SCHEDULE is in the dps map, it's supported.
        # Alternatively, we just add them and let them be unavailable if not supported.
        entities.append(RobovacDndStartTime(item))
        entities.append(RobovacDndEndTime(item))

    async_add_entities(entities)


class RobovacDndTimeBase(RestoreEntity, TimeEntity):
    """Base class for Do Not Disturb Time entities."""

    _attr_has_entity_name = True
    _attr_entity_category = EntityCategory.CONFIG
    _attr_should_poll = False

    def __init__(self, item: dict[str, Any], name: str, is_start: bool) -> None:
        """Initialize the time entity."""
        self.robovac_id = item[CONF_ID]
        self._is_start = is_start
        self._attr_unique_id = f"{item[CONF_ID]}_dnd_{'start' if is_start else 'end'}_time"
        self._attr_name = name
        self._attr_native_value = None

        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, item[CONF_ID])},
            name=item[CONF_NAME]
        )

    async def async_added_to_hass(self) -> None:
        """Handle entity which will be added."""
        await super().async_added_to_hass()
        if (last_state := await self.async_get_last_state()) is not None and last_state.state != "unknown":
            try:
                hour, minute = map(int, last_state.state.split(":"))
                self._attr_native_value = time(hour, minute)
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
        if not vacuum_entity or not vacuum_entity.has_data_or_connected:
            return False
            
        return True

    def _get_raw_schedule_string(self) -> str:
        """Get the raw 9-character schedule string (e.g. 100002359)"""
        vacuum_entity: RoboVacEntity | None = self.hass.data[DOMAIN][CONF_VACS].get(self.robovac_id)
        if not vacuum_entity:
            return "100002359" # Safe default

        dps_code = vacuum_entity.get_dps_code(RobovacCommand.DO_NOT_DISTURB_SCHEDULE)
        if not dps_code or not vacuum_entity.vacuum:
            return "100002359"

        # Read from raw Tuya state
        b64_val = vacuum_entity.vacuum._dps.get(dps_code)
        if b64_val:
            try:
                # Value is likely base64 encoded by the device
                padded_val = str(b64_val) + "=" * (-len(str(b64_val)) % 4)
                val = base64.b64decode(padded_val).decode('utf-8')
                if len(val) == 9:
                    return val
            except Exception:
                pass
                
        return "100002359"

    @property
    def native_value(self) -> time | None:
        """Return the value reported by the time entity."""
        raw_val = self._get_raw_schedule_string()
        if len(raw_val) == 9:
            try:
                if self._is_start:
                    time_str = raw_val[1:5]
                else:
                    time_str = raw_val[5:9]
                    
                hour = int(time_str[:2])
                minute = int(time_str[2:])
                return time(hour, minute)
            except ValueError:
                pass
                
        return self._attr_native_value

    async def async_set_value(self, value: time) -> None:
        """Update the current value."""
        raw_val = self._get_raw_schedule_string()
        enable_flag = raw_val[0] if len(raw_val) == 9 else "1"
        start_str = raw_val[1:5] if len(raw_val) == 9 else "0000"
        end_str = raw_val[5:9] if len(raw_val) == 9 else "2359"

        new_time_str = f"{value.hour:02d}{value.minute:02d}"

        if self._is_start:
            start_str = new_time_str
        else:
            end_str = new_time_str

        new_raw_val = f"{enable_flag}{start_str}{end_str}"
        
        # Base64 encode before sending
        b64_encoded = base64.b64encode(new_raw_val.encode('utf-8')).decode('utf-8')

        vacuum_entity: RoboVacEntity | None = self.hass.data[DOMAIN][CONF_VACS].get(self.robovac_id)
        if vacuum_entity and vacuum_entity.vacuum:
            dps_code = vacuum_entity.get_dps_code(RobovacCommand.DO_NOT_DISTURB_SCHEDULE)
            if dps_code:
                try:
                    await vacuum_entity.vacuum.async_set({dps_code: b64_encoded})
                    self._attr_native_value = value
                    self.async_write_ha_state()
                except Exception as ex:
                    _LOGGER.error("Failed to set DND schedule %s for %s: %s", self._attr_name, self.robovac_id, ex)


class RobovacDndStartTime(RobovacDndTimeBase):
    """Do Not Disturb Start Time."""
    def __init__(self, item: dict[str, Any]) -> None:
        super().__init__(item, "Do Not Disturb Start", True)


class RobovacDndEndTime(RobovacDndTimeBase):
    """Do Not Disturb End Time."""
    def __init__(self, item: dict[str, Any]) -> None:
        super().__init__(item, "Do Not Disturb End", False)
