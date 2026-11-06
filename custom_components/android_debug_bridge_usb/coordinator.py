"""Discover USB phones without blocking the event loop."""

from datetime import timedelta
import logging

from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .bridge import Bridge, UsbDevice
from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)


class UsbCoordinator(DataUpdateCoordinator[dict[str, UsbDevice]]):
    """Track connected phones by unambiguous, nonempty serial number."""

    def __init__(self, hass: HomeAssistant, bridge: Bridge) -> None:
        super().__init__(
            hass, _LOGGER, name=DOMAIN, update_interval=timedelta(seconds=30)
        )
        self.bridge = bridge

    async def _async_update_data(self) -> dict[str, UsbDevice]:
        try:
            devices = await self.hass.async_add_executor_job(self.bridge.devices)
        except Exception as err:
            raise UpdateFailed(f"Cannot enumerate USB devices: {err}") from err
        result: dict[str, UsbDevice] = {}
        for device in devices:
            serial = device["serial"]
            if not isinstance(serial, str) or not serial or serial in result:
                raise UpdateFailed("USB devices have missing or duplicate serial numbers")
            result[serial] = device
        return result
