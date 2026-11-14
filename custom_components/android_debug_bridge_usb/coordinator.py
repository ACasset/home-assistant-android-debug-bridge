"""Discover USB phones without blocking the event loop."""

from collections import Counter
from datetime import timedelta
import logging
import time

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .bridge import Bridge, UsbDevice
from .const import CONF_SERIAL, DOMAIN

_LOGGER = logging.getLogger(__name__)
# A phone that answers is probed rarely to limit USB traffic; one that does not
# is probed on every scan so that an approval on the phone shows up quickly.
PROBE_INTERVAL = 300


class UsbCoordinator(DataUpdateCoordinator[dict[str, UsbDevice]]):
    """Track connected phones by unambiguous, nonempty serial number."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry, bridge: Bridge) -> None:
        super().__init__(
            hass, _LOGGER, name=DOMAIN, config_entry=entry,
            update_interval=timedelta(seconds=30),
        )
        self.bridge = bridge
        self.adb_ready: dict[str, bool] = {}
        self.models: dict[str, str] = {}
        self._probed_at: dict[str, float] = {}
        self._ignored: tuple | None = None

    def resolve_serial(self, serial: str | None) -> str:
        """Return the phone an action targets: explicit, default, or the only one."""
        serial = serial or self.config_entry.options.get(CONF_SERIAL)
        if serial:
            return serial
        if self.data and len(self.data) == 1:
            return next(iter(self.data))
        raise ValueError(
            f"No phone selected and {len(self.data or ())} phones are connected. "
            "Choose a default phone with Configure on the Android Debug Bridge USB "
            "integration, or pass a serial."
        )

    async def _async_update_data(self) -> dict[str, UsbDevice]:
        try:
            devices = await self.hass.async_add_executor_job(self.bridge.devices)
        except Exception as err:
            raise UpdateFailed(f"Cannot enumerate USB devices: {err}") from err
        serials = Counter(
            device["serial"] for device in devices
            if isinstance(device["serial"], str) and device["serial"]
        )
        result = {
            device["serial"]: device for device in devices
            if serials.get(device["serial"]) == 1
        }
        # Phones that cannot be targeted safely are skipped, not fatal to the rest.
        duplicates = sorted(serial for serial, count in serials.items() if count > 1)
        ignored = (len(devices) - sum(serials.values()), *duplicates)
        if ignored != self._ignored:
            self._ignored = ignored
            if ignored[0] or duplicates:
                _LOGGER.warning(
                    "Ignoring %d USB device(s) without a readable serial number and "
                    "devices sharing these serial numbers: %s",
                    ignored[0], ", ".join(duplicates) or "none",
                )

        for serial in set(self.adb_ready) - set(result):
            self._forget(serial)
        now = time.monotonic()
        for serial in result:
            if (
                not self.adb_ready.get(serial)
                or now - self._probed_at[serial] >= PROBE_INTERVAL
            ):
                await self._async_probe(serial)
        return result

    async def _async_probe(self, serial: str) -> None:
        try:
            model = await self.hass.async_add_executor_job(self.bridge.probe, serial)
        except Exception:  # noqa: BLE001 - any failure means "not ready"
            _LOGGER.debug("ADB probe of %s failed", serial, exc_info=True)
            self.adb_ready[serial] = False
            self._probed_at[serial] = time.monotonic()
            return
        if isinstance(model, str):
            self._set_ready(serial)
            self._set_model(serial, model)
            return
        self.adb_ready[serial] = False
        self._probed_at[serial] = time.monotonic()
        ir.async_create_issue(
            self.hass,
            DOMAIN,
            f"adb_unauthorized_{serial}",
            is_fixable=False,
            severity=ir.IssueSeverity.WARNING,
            translation_key="adb_unauthorized",
            translation_placeholders={"serial": serial},
        )

    def _set_ready(self, serial: str) -> None:
        self.adb_ready[serial] = True
        self._probed_at[serial] = time.monotonic()
        ir.async_delete_issue(self.hass, DOMAIN, f"adb_unauthorized_{serial}")

    def _set_model(self, serial: str, model: str) -> None:
        """Remember the model; the device registry keeps it while unplugged."""
        if not model or self.models.get(serial) == model:
            return
        self.models[serial] = model
        registry = dr.async_get(self.hass)
        device = registry.async_get_device(identifiers={(DOMAIN, serial)})
        if device is not None and device.model != model:
            registry.async_update_device(device.id, model=model)

    def _forget(self, serial: str) -> None:
        self.adb_ready.pop(serial, None)
        self._probed_at.pop(serial, None)
        ir.async_delete_issue(self.hass, DOMAIN, f"adb_unauthorized_{serial}")

    @callback
    def async_command_succeeded(self, serial: str) -> None:
        """Record that the phone just answered a command."""
        if self.data and serial in self.data and not self.adb_ready.get(serial):
            self._set_ready(serial)
            self.async_update_listeners()

    @callback
    def async_shutdown_issues(self) -> None:
        """Remove repair issues when the hub is unloaded."""
        for serial in list(self.adb_ready):
            self._forget(serial)
