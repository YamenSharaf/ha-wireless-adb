"""Connection layer: key handling, pairing and TLS connections.

Everything that differs from core `androidtv` in how we reach the device
lives here, so the entity code stays close to upstream.
"""

from collections.abc import Awaitable, Callable
import logging
import os
import re
import ssl
from typing import Any

from adb_shell_wifi.auth.keygen import keygen
from adb_shell_wifi.auth.x509 import (
    certificate_to_pem,
    generate_x509_certificate,
    load_rsa_private_key_pem,
    private_key_to_pem,
)
from adb_shell_wifi.pairing import PairingException, pair_async
from adb_shell_wifi.transport import tls_transport_async
from androidtv_wifi.setup_async import AndroidTVAsync

from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import STORAGE_DIR

from .const import ADBKEY_FILENAME

_LOGGER = logging.getLogger(__name__)

CONNECTION_TYPE_TLS = "tls"
AUTH_TIMEOUT_S = 10.0
PAIRING_TIMEOUT_S = 15.0

__all__ = ["PairingException"]


# --------------------------------------------------------------------------- #
# ADB key
# --------------------------------------------------------------------------- #


def default_adbkey_path(hass: HomeAssistant) -> str:
    """Return where the integration keeps its own ADB key."""
    return hass.config.path(STORAGE_DIR, ADBKEY_FILENAME)


def _ensure_adbkey(path: str) -> None:
    if not os.path.isfile(path):
        _LOGGER.info("Generating a new ADB key at %s", path)
        keygen(path)


def _read_keypair(path: str) -> tuple[bytes, bytes]:
    with open(path, "rb") as f:
        private_key = f.read()
    with open(f"{path}.pub", "rb") as f:
        public_key = f.read()
    return private_key, public_key


async def async_ensure_adbkey(hass: HomeAssistant, path: str) -> None:
    """Generate the ADB key pair if it does not exist yet."""
    await hass.async_add_executor_job(_ensure_adbkey, path)


# --------------------------------------------------------------------------- #
# TLS context cache
#
# adb_shell_wifi builds a fresh SSLContext on every TLS connect, writing the
# cert and key to temp files and calling load_cert_chain() inside the event
# loop. Home Assistant flags that as a blocking call. The device only checks
# the public key, so one context per private key is enough: build it once in
# the executor (async_prewarm_tls_context) and hand it out from the cache.
# --------------------------------------------------------------------------- #

_original_make_ssl_context = tls_transport_async._make_ssl_context  # noqa: SLF001
_ssl_context_cache: dict[bytes, ssl.SSLContext] = {}


def _cached_make_ssl_context(cert_pem: bytes, key_pem: bytes) -> ssl.SSLContext:
    if (ctx := _ssl_context_cache.get(key_pem)) is None:
        ctx = _ssl_context_cache[key_pem] = _original_make_ssl_context(
            cert_pem, key_pem
        )
    return ctx


tls_transport_async._make_ssl_context = _cached_make_ssl_context  # noqa: SLF001


def _prewarm_tls_context(adbkey: str) -> None:
    with open(adbkey, "rb") as f:
        rsa_key = load_rsa_private_key_pem(f.read())
    # Same derivation adb_shell_wifi uses in its STLS upgrade.
    cert_pem = certificate_to_pem(generate_x509_certificate(rsa_key))
    _cached_make_ssl_context(cert_pem, private_key_to_pem(rsa_key))


async def async_prewarm_tls_context(hass: HomeAssistant, adbkey: str) -> None:
    """Build and cache the TLS context for this key outside the event loop."""
    await hass.async_add_executor_job(_prewarm_tls_context, adbkey)


# --------------------------------------------------------------------------- #
# stderr filter
#
# Some builds (e.g. Sony Bravia, Android 14) print "Failed to write while
# dumping service <x>: Broken pipe" to stderr when a `dumpsys <x> | grep -q`
# pipeline closes early. `adb shell` merges stderr into stdout, which corrupts
# the positional parsing in androidtv's update() (the TV reads as "off").
# Discarding dumpsys' stderr fixes it.
# --------------------------------------------------------------------------- #

_DUMPSYS_PIPE = re.compile(r"(dumpsys\b[^|;&]*?)\s*\|(?!\|)")


def filter_dumpsys_stderr(cmd: str) -> str:
    """Add `2>/dev/null` to every `dumpsys ... |` that doesn't redirect stderr."""

    def _repl(match: re.Match[str]) -> str:
        head = match.group(1)
        if "2>" in head:
            return match.group(0)
        return f"{head} 2>/dev/null |"

    return _DUMPSYS_PIPE.sub(_repl, cmd)


def _install_stderr_filter(aftv: AndroidTVAsync) -> None:
    adb = aftv._adb  # noqa: SLF001
    shell: Callable[[str], Awaitable[Any]] = adb.shell

    async def _filtered_shell(cmd: str) -> Any:
        return await shell(filter_dumpsys_stderr(cmd))

    adb.shell = _filtered_shell


# --------------------------------------------------------------------------- #
# Pairing and connecting
# --------------------------------------------------------------------------- #


async def async_pair(
    hass: HomeAssistant, host: str, pairing_port: int, pairing_code: str, adbkey: str
) -> None:
    """Pair our key with the device using the code from its pairing dialog.

    Raises PairingException (wrong code, dialog closed) or OSError
    (unreachable).
    """
    await async_ensure_adbkey(hass, adbkey)
    private_key, public_key = await hass.async_add_executor_job(_read_keypair, adbkey)
    await pair_async(
        host=host,
        port=pairing_port,
        pairing_code=pairing_code,
        private_key_pem=private_key,
        public_key=public_key,
        timeout_s=PAIRING_TIMEOUT_S,
    )


async def async_connect(
    hass: HomeAssistant,
    host: str,
    port: int,
    adbkey: str,
    *,
    state_detection_rules: dict[str, Any] | None = None,
) -> AndroidTVAsync:
    """Open a wireless-debugging (TLS) session.

    Always returns the device object; check `.available` for success. On
    success, device properties and installed apps are loaded, mirroring
    androidtv's setup().
    """
    await async_ensure_adbkey(hass, adbkey)
    await async_prewarm_tls_context(hass, adbkey)

    aftv = AndroidTVAsync(
        host,
        port,
        adbkey=adbkey,
        state_detection_rules=state_detection_rules,
        connection_type=CONNECTION_TYPE_TLS,
    )
    _install_stderr_filter(aftv)

    if await aftv.adb_connect(log_errors=False, auth_timeout_s=AUTH_TIMEOUT_S):
        await aftv.get_device_properties()
        await aftv.get_installed_apps()
    return aftv
