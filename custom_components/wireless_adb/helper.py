"""Install, update and maintain the Wireless ADB Helper app on the device.

The helper (android/ in this repository) turns wireless debugging back on at
boot and every 15 minutes. It needs WRITE_SECURE_SETTINGS, which only ADB can
grant, so the integration is the natural place to install it: it already has
an ADB shell on the device.

Release zips carry the APK in helper/ next to helper.json, which the release
workflow writes:

    {"version_name": "2.2.0", "version_code": 105, "sha256": "…"}

A development checkout has no helper/ folder; then nothing is offered.
"""

from dataclasses import dataclass
import json
import logging
from pathlib import Path
import re

from androidtv_wifi.setup_async import AndroidTVAsync

from homeassistant.core import HomeAssistant

from .const import (
    HELPER_APK,
    HELPER_DIR,
    HELPER_INFO,
    HELPER_LAUNCH_URI,
    HELPER_PACKAGE,
)

_LOGGER = logging.getLogger(__name__)

_DEVICE_TMP = f"/data/local/tmp/{HELPER_APK}"
_PERMISSIONS = (
    "android.permission.WRITE_SECURE_SETTINGS",
    "android.permission.POST_NOTIFICATIONS",
)


class HelperError(Exception):
    """Installing or configuring the helper failed."""


@dataclass(frozen=True)
class HelperVersion:
    """A helper build, identified by versionCode (versionName is for display)."""

    name: str
    code: int

    def __str__(self) -> str:
        """Return e.g. '2.2.0 (105)'."""
        return f"{self.name} ({self.code})"


@dataclass(frozen=True)
class BundledHelper:
    """The APK shipped with this integration release."""

    version: HelperVersion
    apk: Path


@dataclass(frozen=True)
class InstalledHelper:
    """What `dumpsys package` reports for the helper on the device."""

    version: HelperVersion | None  # None: not installed
    granted: bool  # WRITE_SECURE_SETTINGS

    @property
    def installed(self) -> bool:
        """Return True if the helper is installed."""
        return self.version is not None


def _load_bundled() -> BundledHelper | None:
    base = Path(__file__).parent / HELPER_DIR
    info_file, apk = base / HELPER_INFO, base / HELPER_APK
    if not (info_file.is_file() and apk.is_file()):
        return None
    try:
        info = json.loads(info_file.read_text())
        version = HelperVersion(str(info["version_name"]), int(info["version_code"]))
    except (ValueError, KeyError, TypeError) as err:
        _LOGGER.warning(
            "Ignoring the bundled helper, %s is invalid: %s", info_file, err
        )
        return None
    return BundledHelper(version, apk)


async def async_get_bundled(hass: HomeAssistant) -> BundledHelper | None:
    """Return the helper bundled with this release, if any."""
    return await hass.async_add_executor_job(_load_bundled)


_VERSION_CODE = re.compile(r"versionCode=(\d+)")
_VERSION_NAME = re.compile(r"versionName=(\S+)")
_GRANTED = re.compile(
    r"android\.permission\.WRITE_SECURE_SETTINGS: granted=(true|false)"
)


async def async_get_installed(aftv: AndroidTVAsync) -> InstalledHelper:
    """Read the installed helper's version and permission state."""
    output = await aftv.adb_shell(f"dumpsys package {HELPER_PACKAGE}") or ""
    code, name = _VERSION_CODE.search(output), _VERSION_NAME.search(output)
    if not code:
        return InstalledHelper(version=None, granted=False)
    granted = _GRANTED.findall(output)
    return InstalledHelper(
        version=HelperVersion(name.group(1) if name else "?", int(code.group(1))),
        # Runtime and install grants can both be listed; any true counts.
        granted="true" in granted,
    )


async def async_grant_and_launch(aftv: AndroidTVAsync) -> None:
    """Grant the helper's permissions and launch it once.

    Launching clears Android's "stopped" state (a never-launched app gets no
    BOOT_COMPLETED) and starts the watchdog.
    """
    for permission in _PERMISSIONS:
        result = await aftv.adb_shell(f"pm grant {HELPER_PACKAGE} {permission} 2>&1")
        if result and result.strip():
            # pm grant prints nothing on success.
            raise HelperError(f"pm grant {permission} failed: {result.strip()}")
    await aftv.adb_shell(
        f"am start -a android.intent.action.VIEW -d {HELPER_LAUNCH_URI} 2>&1"
    )


async def async_install(aftv: AndroidTVAsync, bundled: BundledHelper) -> None:
    """Install or update the helper in place, grant, and launch it."""
    _LOGGER.info("Installing Wireless ADB Helper %s", bundled.version)
    await aftv.adb_push(str(bundled.apk), _DEVICE_TMP)
    try:
        result = await aftv.adb_shell(f"pm install -r {_DEVICE_TMP} 2>&1") or ""
    finally:
        await aftv.adb_shell(f"rm -f {_DEVICE_TMP}")
    if "Success" not in result:
        # e.g. INSTALL_FAILED_UPDATE_INCOMPATIBLE: installed with another key.
        raise HelperError(f"pm install failed: {result.strip() or 'no output'}")
    await async_grant_and_launch(aftv)
