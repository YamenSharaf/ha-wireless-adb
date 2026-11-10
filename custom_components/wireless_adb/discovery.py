"""Find a device's wireless-debugging TLS port over mDNS.

Wireless debugging listens on a random port that changes whenever adbd
restarts (reboot, toggling wireless debugging, `adb tcpip`). The device
advertises the current port as an `_adb-tls-connect._tcp` service named
`adb-<serial>-<random>`. Home Assistant's zeroconf discovery normally tells
us about changes (see config_flow.async_step_zeroconf); this module is the
active fallback, used when a connect fails.

Matching is by serial (or host) only, never "first service found": phones
and other devices on the LAN advertise the same service type.
"""

import asyncio
import logging

from zeroconf import ServiceStateChange, Zeroconf
from zeroconf.asyncio import AsyncServiceBrowser, AsyncServiceInfo

from homeassistant.components import zeroconf
from homeassistant.core import HomeAssistant

from .const import SERVICE_TYPE_CONNECT

_LOGGER = logging.getLogger(__name__)

_INSTANCE_PREFIX = "adb-"


def serial_from_service_name(name: str) -> str | None:
    """Return the serial from 'adb-<serial>-<random>[._adb-tls-connect._tcp.local.]'."""
    instance = name.split(".", 1)[0]
    if not instance.startswith(_INSTANCE_PREFIX):
        return None
    serial, sep, _ = instance[len(_INSTANCE_PREFIX) :].rpartition("-")
    return serial if sep and serial else None


async def async_find_connect_endpoint(
    hass: HomeAssistant,
    *,
    serial: str | None = None,
    host: str | None = None,
    timeout: float = 5.0,
) -> tuple[str, int] | None:
    """Browse for the device's TLS endpoint, matched by serial or host.

    Returns (host, port) of the first matching service, or None if nothing
    matches within `timeout` seconds.
    """
    if serial is None and host is None:
        raise ValueError("serial or host is required")

    aiozc = await zeroconf.async_get_async_instance(hass)
    found: asyncio.Future[tuple[str, int]] = hass.loop.create_future()
    pending: set[asyncio.Task[None]] = set()

    async def _resolve(name: str) -> None:
        info = AsyncServiceInfo(SERVICE_TYPE_CONNECT, name)
        if not await info.async_request(aiozc.zeroconf, 3000):
            return
        addresses = info.parsed_addresses()
        if not addresses or info.port is None:
            return
        if host is not None and host not in addresses:
            return
        if not found.done():
            # Prefer the requested host, else the first (IPv4 sorts first).
            found.set_result((host or addresses[0], info.port))

    def _on_change(
        zeroconf: Zeroconf,
        service_type: str,
        name: str,
        state_change: ServiceStateChange,
    ) -> None:
        if state_change is ServiceStateChange.Removed or found.done():
            return
        if serial is not None and serial_from_service_name(name) != serial:
            return
        task = hass.async_create_background_task(
            _resolve(name), f"wireless_adb resolve {name}"
        )
        pending.add(task)
        task.add_done_callback(pending.discard)

    browser = AsyncServiceBrowser(
        aiozc.zeroconf, SERVICE_TYPE_CONNECT, handlers=[_on_change]
    )
    try:
        async with asyncio.timeout(timeout):
            return await found
    except TimeoutError:
        _LOGGER.debug(
            "No %s service found for serial=%s host=%s within %ss",
            SERVICE_TYPE_CONNECT,
            serial,
            host,
            timeout,
        )
        return None
    finally:
        await browser.async_cancel()
        for task in pending:
            task.cancel()
