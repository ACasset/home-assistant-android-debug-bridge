"""Connection status for discovered USB phones."""

from homeassistant.components.binary_sensor import BinarySensorDeviceClass, BinarySensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import UsbCoordinator


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    """Add existing and newly connected phones."""
    coordinator: UsbCoordinator = hass.data[DOMAIN]
    known: set[str] = set()
    registry = dr.async_get(hass)
    previous = {
        serial
        for device in dr.async_entries_for_config_entry(registry, entry.entry_id)
        for domain, serial in device.identifiers
        if domain == DOMAIN
    }

    @callback
    def async_discover() -> None:
        new = (set(coordinator.data) | previous) - known
        known.update(new)
        async_add_entities(
            entity(coordinator, serial)
            for serial in sorted(new)
            for entity in (UsbConnected, AdbReady)
        )

    entry.async_on_unload(coordinator.async_add_listener(async_discover))
    async_discover()


class PhoneEntity(CoordinatorEntity[UsbCoordinator], BinarySensorEntity):
    """Base for the sensors of one phone."""

    _attr_has_entity_name = True

    def __init__(self, coordinator: UsbCoordinator, serial: str) -> None:
        super().__init__(coordinator)
        self.serial = serial
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, serial)},
            name=f"Android {serial}",
            manufacturer="Android",
            serial_number=serial,
        )


class UsbConnected(PhoneEntity):
    """Report USB presence, not ADB authorization or shell command success."""

    _attr_name = "USB connected"
    _attr_device_class = BinarySensorDeviceClass.CONNECTIVITY

    def __init__(self, coordinator: UsbCoordinator, serial: str) -> None:
        super().__init__(coordinator, serial)
        self._attr_unique_id = f"{serial}_usb_connected"

    @property
    def is_on(self) -> bool:
        return self.serial in self.coordinator.data

    @property
    def extra_state_attributes(self) -> dict:
        device = self.coordinator.data.get(self.serial, {})
        return {"serial": self.serial, "port_path": device.get("port_path")}


class AdbReady(PhoneEntity):
    """Report whether the connected phone trusts this host and answers ADB."""

    _attr_name = "ADB ready"
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, coordinator: UsbCoordinator, serial: str) -> None:
        super().__init__(coordinator, serial)
        self._attr_unique_id = f"{serial}_adb_ready"

    @property
    def available(self) -> bool:
        return super().available and self.serial in self.coordinator.data

    @property
    def is_on(self) -> bool:
        return self.coordinator.adb_ready.get(self.serial, False)
