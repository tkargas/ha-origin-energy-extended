"""Sensors for Origin Energy.

The actual energy/cost data goes into HA's long-term statistics
(see coordinator.py) and appears in the Energy dashboard directly --
it isn't backed by an entity. The last-update sensor surfaces when that last
successfully ran, for dashboards/automations/troubleshooting. The meter total
sensor is a plain number to look at: it deliberately has no state class, so
Home Assistant records no statistics for it and the Energy dashboard can't
pick it up and count the same usage twice.
"""
from __future__ import annotations

from homeassistant.components.sensor import SensorDeviceClass, SensorEntity
from homeassistant.const import UnitOfEnergy
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
    async_add_entities(
        [
            OriginEnergyLastUpdateSensor(entry.runtime_data),
            OriginEnergyMeterTotalSensor(entry.runtime_data),
        ]
    )


def _device_info(coordinator: OriginEnergyCoordinator) -> DeviceInfo:
    # has_entity_name only produces a sensible entity_id when there is a
    # device to take the name from; without this the entity would be
    # plain "sensor.last_update" and collide across accounts.
    return DeviceInfo(
        identifiers={(DOMAIN, coordinator.agreement_id)},
        entry_type=DeviceEntryType.SERVICE,
        manufacturer="Origin Energy",
        name="Origin Energy",
    )


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
        self._attr_device_info = _device_info(coordinator)

    @property
    def native_value(self):
        if self.coordinator.data is None:
            return None
        return self.coordinator.data.last_updated


class OriginEnergyMeterTotalSensor(
    CoordinatorEntity[OriginEnergyCoordinator], SensorEntity
):
    """Electricity meter total: the last manual reading plus Origin's usage since.

    Without a manual statistic it is simply the total Origin has reported.
    """

    _attr_has_entity_name = True
    _attr_translation_key = "meter_total"
    _attr_device_class = SensorDeviceClass.ENERGY
    _attr_native_unit_of_measurement = UnitOfEnergy.KILO_WATT_HOUR
    _attr_suggested_display_precision = 2

    def __init__(self, coordinator: OriginEnergyCoordinator) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{coordinator.agreement_id}_meter_total"
        self._attr_device_info = _device_info(coordinator)

    @property
    def native_value(self):
        data = self.coordinator.data
        if data is None or data.meter_total is None:
            return None
        return round(data.meter_total.total, 3)

    @property
    def extra_state_attributes(self):
        data = self.coordinator.data
        if data is None or data.meter_total is None:
            return None
        total = data.meter_total
        return {
            "last_manual_reading": total.reading,
            "last_manual_day": total.reading_day.isoformat() if total.reading_day else None,
            "origin_usage_since": round(total.origin_kwh, 3),
        }
