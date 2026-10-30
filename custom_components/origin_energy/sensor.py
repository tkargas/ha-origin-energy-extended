"""Diagnostic sensor for Origin Energy.

The actual energy/cost data goes into HA's long-term statistics
(see coordinator.py) and appears in the Energy dashboard directly --
it isn't backed by an entity. This sensor just surfaces when that last
successfully ran, for dashboards/automations/troubleshooting.
"""
from __future__ import annotations

from homeassistant.components.sensor import SensorDeviceClass, SensorEntity
from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import OriginEnergyConfigEntry, OriginEnergyCoordinator


async def async_setup_entry(
    hass,
    entry: OriginEnergyConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    async_add_entities([OriginEnergyLastUpdateSensor(entry.runtime_data)])


class OriginEnergyLastUpdateSensor(
    CoordinatorEntity[OriginEnergyCoordinator], SensorEntity
):
    """Timestamp of the last successful Origin Energy statistics import."""

    _attr_has_entity_name = True
    _attr_translation_key = "last_update"
    _attr_device_class = SensorDeviceClass.TIMESTAMP

    def __init__(self, coordinator: OriginEnergyCoordinator) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{coordinator.agreement_id}_last_update"
        # has_entity_name only produces a sensible entity_id when there is a
        # device to take the name from; without this the entity would be
        # plain "sensor.last_update" and collide across accounts.
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, coordinator.agreement_id)},
            entry_type=DeviceEntryType.SERVICE,
            manufacturer="Origin Energy",
            name="Origin Energy",
        )

    @property
    def native_value(self):
        if self.coordinator.data is None:
            return None
        return self.coordinator.data.last_updated
