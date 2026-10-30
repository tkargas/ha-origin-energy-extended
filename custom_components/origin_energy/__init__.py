"""The Origin Energy integration."""
from __future__ import annotations

from homeassistant.core import HomeAssistant

from .coordinator import OriginEnergyConfigEntry, OriginEnergyCoordinator

PLATFORMS = ["sensor"]


async def _async_entry_updated(
    hass: HomeAssistant, entry: OriginEnergyConfigEntry
) -> None:
    """Apply a poll interval or manual statistic changed from the Configure dialog.

    Deliberately not a reload. This listener fires for every write to the
    entry, including the coordinator persisting a rolled session cookie
    during setup -- reloading on that would set up the entry, roll the
    cookie, and reload again, indefinitely.

    Assigning update_interval stores the value but does not reschedule
    anything, so an already-queued refresh would still fire on the old
    timing; requesting one now makes the change take effect immediately,
    since the reschedule that follows a refresh uses the new interval.
    """
    coordinator = entry.runtime_data
    interval_changed = coordinator.async_adopt_update_interval()
    manual_changed = coordinator.async_adopt_manual_statistic()
    if interval_changed or manual_changed:
        await coordinator.async_request_refresh()


async def async_setup_entry(hass: HomeAssistant, entry: OriginEnergyConfigEntry) -> bool:
    """Set up Origin Energy from a config entry."""
    coordinator = OriginEnergyCoordinator(hass, entry)
    await coordinator.async_config_entry_first_refresh()

    entry.runtime_data = coordinator
    # Registered only once runtime_data exists: the listener reads it, and
    # the first refresh above can itself write to the entry.
    entry.async_on_unload(entry.add_update_listener(_async_entry_updated))
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: OriginEnergyConfigEntry) -> bool:
    """Unload an Origin Energy config entry."""
    unloaded = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unloaded:
        await entry.runtime_data.async_close()
    return unloaded
