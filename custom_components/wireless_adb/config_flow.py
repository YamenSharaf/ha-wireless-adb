"""Config flow for Wireless ADB.

Setup is: host -> pairing code -> connect. Devices with wireless debugging on
are also discovered over zeroconf (`_adb-tls-connect._tcp`); for a device
that's already configured, that discovery is how a rotated port reaches the
config entry.
"""

import asyncio
from collections.abc import Mapping
import logging
import re
from typing import Any, override

from androidtv_wifi import state_detection_rules_validator
from androidtv_wifi.setup_async import AndroidTVAsync
import probatio

from homeassistant.config_entries import (
    ConfigEntry,
    ConfigEntryState,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlow,
)
from homeassistant.const import CONF_HOST, CONF_PORT
from homeassistant.core import callback
from homeassistant.helpers.selector import (
    EntitySelector,
    EntitySelectorConfig,
    ObjectSelector,
    SelectOptionDict,
    SelectSelector,
    SelectSelectorConfig,
    SelectSelectorMode,
    TextSelector,
    TextSelectorConfig,
    TextSelectorType,
)
from homeassistant.helpers.service_info.zeroconf import ZeroconfServiceInfo

from .connection import PairingException, async_connect, async_pair, default_adbkey_path
from .const import (
    CONF_APPS,
    CONF_DEVICE_PROPERTIES,
    CONF_EXCLUDE_UNNAMED_APPS,
    CONF_GET_SOURCES,
    CONF_INSTALL_HELPER,
    CONF_KEEP_PAIRING,
    CONF_PAIRING_CODE,
    CONF_PAIRING_PORT,
    CONF_RELAUNCH_ENTITY,
    CONF_RELAUNCH_HELPER,
    CONF_SCREENCAP_INTERVAL,
    CONF_STATE_DETECTION_RULES,
    CONF_TURN_OFF_COMMAND,
    CONF_TURN_ON_COMMAND,
    DEFAULT_EXCLUDE_UNNAMED_APPS,
    DEFAULT_GET_SOURCES,
    DEFAULT_KEEP_PAIRING,
    DEFAULT_RELAUNCH_HELPER,
    DEFAULT_SCREENCAP_INTERVAL,
    DOMAIN,
    PROP_SERIALNO,
)
from .discovery import async_find_connect_endpoint, serial_from_service_name
from .helper import BundledHelper, async_get_bundled, async_get_installed, async_install

APPS_NEW_ID = "NewApp"
CONF_APP_DELETE = "app_delete"
CONF_APP_ID = "app_id"
CONF_APP_NAME = "app_name"

RULES_NEW_ID = "NewRule"
CONF_RULE_DELETE = "rule_delete"
CONF_RULE_ID = "rule_id"
CONF_RULE_VALUES = "rule_values"

RESULT_CONN_ERROR = "cannot_connect"
RESULT_UNKNOWN = "unknown"

CONNECT_ATTEMPTS = 3
CONNECT_RETRY_DELAY_S = 2

# The pairing code is six digits, but stays a string: it can start with 0.
# TEL brings up the numeric keypad on phones.
_PAIRING_CODE_SELECTOR = TextSelector(
    TextSelectorConfig(type=TextSelectorType.TEL, autocomplete="off")
)
_PAIRING_CODE = re.compile(r"\d{6}")

_PAIR_SCHEMA_OPTIONAL = probatio.Schema(
    {
        probatio.Optional(CONF_PAIRING_PORT): probatio.Port(),
        probatio.Optional(CONF_PAIRING_CODE): _PAIRING_CODE_SELECTOR,
    }
)
_PAIR_SCHEMA_REQUIRED = probatio.Schema(
    {
        probatio.Required(CONF_PAIRING_PORT): probatio.Port(),
        probatio.Required(CONF_PAIRING_CODE): _PAIRING_CODE_SELECTOR,
    }
)

_LOGGER = logging.getLogger(__name__)


class WirelessAdbFlowHandler(ConfigFlow, domain=DOMAIN):
    """Handle a config flow."""

    VERSION = 1

    def __init__(self) -> None:
        """Initialize the flow."""
        self._host: str | None = None
        self._port: int | None = None  # TLS port, when known from zeroconf
        self._aftv: AndroidTVAsync | None = None  # open session, setup only
        self._title: str | None = None
        self._bundled: BundledHelper | None = None

    @override
    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Ask for the device's address."""
        if user_input is not None:
            self._host = user_input[CONF_HOST].strip()
            self._async_abort_entries_match({CONF_HOST: self._host})
            return await self.async_step_pair()

        return self.async_show_form(
            step_id="user",
            data_schema=probatio.Schema({probatio.Required(CONF_HOST): str}),
        )

    async def async_step_zeroconf(
        self, discovery_info: ZeroconfServiceInfo
    ) -> ConfigFlowResult:
        """Handle a device advertising wireless debugging."""
        serial = serial_from_service_name(discovery_info.name)
        if serial is None:
            return self.async_abort(reason="not_wireless_debugging")

        host, port = discovery_info.host, discovery_info.port
        if port is None:
            return self.async_abort(reason="not_wireless_debugging")

        if entry := await self.async_set_unique_id(serial):
            # Known device: wireless debugging (re)started on a new port.
            if (entry.data.get(CONF_HOST), entry.data.get(CONF_PORT)) != (host, port):
                _LOGGER.debug("%s now advertises %s:%s", entry.title, host, port)
                self.hass.config_entries.async_update_entry(
                    entry, data={**entry.data, CONF_HOST: host, CONF_PORT: port}
                )
                # A loaded entry reloads via its update listener; one that
                # is retrying setup needs a nudge to use the new port now.
                if entry.state is not ConfigEntryState.LOADED:
                    self.hass.config_entries.async_schedule_reload(entry.entry_id)
            return self.async_abort(reason="already_configured")

        self._host, self._port = host, port
        self.context["title_placeholders"] = {"name": f"{serial} ({host})"}
        return await self.async_step_pair()

    async def async_step_pair(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Pair with the code from the device's pairing dialog, then connect."""
        assert self._host is not None
        errors: dict[str, str] = {}

        if user_input is not None:
            errors = await self._async_pair(user_input, code_required=False)
            if not errors:
                result = await self._async_connect_and_create()
                if isinstance(result, str):
                    errors["base"] = result
                else:
                    return result

        return self.async_show_form(
            step_id="pair",
            data_schema=_PAIR_SCHEMA_OPTIONAL,
            description_placeholders={"host": self._host},
            errors=errors,
        )

    async def async_step_reauth(
        self, entry_data: Mapping[str, Any]
    ) -> ConfigFlowResult:
        """The device stopped accepting our key: pair it again."""
        self._host = entry_data[CONF_HOST]
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Ask for a new pairing code."""
        assert self._host is not None
        entry = self._get_reauth_entry()
        errors: dict[str, str] = {}

        if user_input is not None:
            errors = await self._async_pair(user_input, code_required=True)
            if not errors:
                aftv, port = await self._async_open_session()
                if aftv is None:
                    errors["base"] = "not_advertised"
                elif not aftv.available:
                    errors["base"] = RESULT_CONN_ERROR
                else:
                    props = dict(aftv.device_properties)
                    await aftv.adb_close()
                    if props.get(PROP_SERIALNO) not in (None, entry.unique_id):
                        return self.async_abort(reason="wrong_device")
                    return self.async_update_reload_and_abort(
                        entry,
                        data_updates={
                            CONF_HOST: self._host,
                            CONF_PORT: port,
                            CONF_DEVICE_PROPERTIES: props,
                        },
                    )

        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=_PAIR_SCHEMA_REQUIRED,
            description_placeholders={"host": self._host, "name": entry.title},
            errors=errors,
        )

    async def async_step_helper(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Offer to install (or update) the helper app, then create the entry."""
        assert self._aftv is not None and self._bundled is not None
        errors: dict[str, str] = {}

        if user_input is not None:
            if not user_input.get(CONF_INSTALL_HELPER):
                return await self._async_create_entry()
            try:
                await async_install(self._aftv, self._bundled)
            except Exception as err:  # noqa: BLE001 - shown to the user, can be skipped
                _LOGGER.warning("Installing the helper app failed: %s", err)
                errors["base"] = "helper_install_failed"
            else:
                return await self._async_create_entry()

        return self.async_show_form(
            step_id="helper",
            data_schema=probatio.Schema(
                {probatio.Required(CONF_INSTALL_HELPER, default=True): bool}
            ),
            description_placeholders={"version": str(self._bundled.version)},
            errors=errors,
        )

    async def _async_pair(
        self, user_input: dict[str, Any], *, code_required: bool
    ) -> dict[str, str]:
        """Pair if a code was given; return form errors."""
        assert self._host is not None
        # Tolerate "123 456" and stray spaces from copy-paste.
        code = "".join((user_input.get(CONF_PAIRING_CODE) or "").split())
        pairing_port = user_input.get(CONF_PAIRING_PORT)

        # During setup pairing is optional: leave the code empty if this Home
        # Assistant is already paired. Re-authentication always needs it.
        if not code:
            return {CONF_PAIRING_CODE: "pairing_code_required"} if code_required else {}
        if not _PAIRING_CODE.fullmatch(code):
            return {CONF_PAIRING_CODE: "invalid_pairing_code"}
        if not pairing_port:
            return {CONF_PAIRING_PORT: "pairing_port_required"}
        try:
            await async_pair(
                self.hass,
                self._host,
                int(pairing_port),
                code,
                default_adbkey_path(self.hass),
            )
        except PairingException as err:
            _LOGGER.warning("Pairing with %s failed: %s", self._host, err)
            return {"base": "pairing_failed"}
        except OSError as err:
            _LOGGER.warning(
                "Could not reach the pairing port %s:%s: %s",
                self._host,
                pairing_port,
                err,
            )
            return {"base": "pairing_unreachable"}
        except Exception:
            _LOGGER.exception("Unexpected error pairing with %s", self._host)
            return {"base": RESULT_UNKNOWN}
        return {}

    async def _async_open_session(self) -> tuple[AndroidTVAsync | None, int | None]:
        """Connect over TLS; returns (session or None if never advertised, port).

        Opening the pairing dialog restarts wireless debugging on a new port,
        so a port remembered from discovery is often stale by now. Try it once,
        then look the current port up (a few times, since the new
        advertisement can lag the dialog).
        """
        assert self._host is not None
        adbkey = default_adbkey_path(self.hass)
        aftv: AndroidTVAsync | None = None
        port = self._port
        for attempt in range(CONNECT_ATTEMPTS):
            if port is None:
                found = await async_find_connect_endpoint(self.hass, host=self._host)
                if found is None:
                    await asyncio.sleep(CONNECT_RETRY_DELAY_S)
                    continue
                port = found[1]
            aftv = await async_connect(self.hass, self._host, port, adbkey)
            if aftv.available:
                return aftv, port
            _LOGGER.debug(
                "Attempt %s: no session on %s:%s", attempt + 1, self._host, port
            )
            port = None
            await asyncio.sleep(CONNECT_RETRY_DELAY_S)
        return aftv, port

    async def _async_connect_and_create(self) -> ConfigFlowResult | str:
        """Connect over TLS, then offer the helper or create the entry.

        Returns an error key for the form on failure.
        """
        assert self._host is not None
        try:
            aftv, port = await self._async_open_session()
        except Exception:
            _LOGGER.exception("Unexpected error connecting to %s", self._host)
            return RESULT_UNKNOWN
        if aftv is None:
            return "not_advertised"
        if not aftv.available:
            return RESULT_CONN_ERROR

        props = dict(aftv.device_properties)
        serial = props.get(PROP_SERIALNO)
        if not serial:
            await aftv.adb_close()
            return self.async_abort(reason="invalid_unique_id")

        await self.async_set_unique_id(serial, raise_on_progress=False)
        if any(entry.unique_id == serial for entry in self._async_current_entries()):
            await aftv.adb_close()  # about to abort as already configured
        self._abort_if_unique_id_configured(
            updates={CONF_HOST: self._host, CONF_PORT: port}
        )

        self._aftv, self._port = aftv, port
        self._title = (
            " ".join(
                part for part in (props.get("manufacturer"), props.get("model")) if part
            )
            or self._host
        )

        # Offer the helper app if it's missing, outdated or lacks its grant.
        self._bundled = await async_get_bundled(self.hass)
        if self._bundled is not None:
            try:
                installed = await async_get_installed(aftv)
            except Exception as err:  # noqa: BLE001 - not worth failing setup over
                _LOGGER.debug("Could not read the helper state: %r", err)
            else:
                if (
                    installed.version is None
                    or installed.version.code < self._bundled.version.code
                    or not installed.granted
                ):
                    return await self.async_step_helper()
        return await self._async_create_entry()

    async def _async_create_entry(self) -> ConfigFlowResult:
        """Close the setup session and create the entry."""
        assert self._aftv is not None and self._title is not None
        props = dict(self._aftv.device_properties)
        await self._aftv.adb_close()
        self._aftv = None
        return self.async_create_entry(
            title=self._title,
            data={
                CONF_HOST: self._host,
                CONF_PORT: self._port,
                CONF_DEVICE_PROPERTIES: props,
            },
        )

    @callback
    @override
    def async_remove(self) -> None:
        """Close a session left open by an abandoned flow."""
        if self._aftv is not None:
            self.hass.async_create_task(self._aftv.adb_close())
            self._aftv = None

    @staticmethod
    @callback
    @override
    def async_get_options_flow(config_entry: ConfigEntry) -> OptionsFlowHandler:
        """Get the options flow for this handler."""
        return OptionsFlowHandler(config_entry)


class OptionsFlowHandler(OptionsFlow):
    """Handle an option flow for Wireless ADB."""

    def __init__(self, config_entry: ConfigEntry) -> None:
        """Initialize options flow."""
        self._apps: dict[str, Any] = dict(config_entry.options.get(CONF_APPS, {}))
        self._state_det_rules: dict[str, Any] = dict(
            config_entry.options.get(CONF_STATE_DETECTION_RULES, {})
        )
        self._conf_app_id: str | None = None
        self._conf_rule_id: str | None = None

    @callback
    def _save_config(self, data: dict[str, Any]) -> ConfigFlowResult:
        """Save the updated options."""
        new_data = {
            k: v
            for k, v in data.items()
            if k not in [CONF_APPS, CONF_STATE_DETECTION_RULES]
        }
        if self._apps:
            new_data[CONF_APPS] = self._apps
        if self._state_det_rules:
            new_data[CONF_STATE_DETECTION_RULES] = self._state_det_rules

        return self.async_create_entry(title="", data=new_data)

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Handle options flow."""
        if user_input is not None:
            if sel_app := user_input.get(CONF_APPS):
                return await self.async_step_apps(None, sel_app)
            if sel_rule := user_input.get(CONF_STATE_DETECTION_RULES):
                return await self.async_step_rules(None, sel_rule)
            return self._save_config(user_input)

        return self._async_init_form()

    @callback
    def _async_init_form(self) -> ConfigFlowResult:
        """Return initial configuration form."""

        apps_list = {k: f"{v} ({k})" if v else k for k, v in self._apps.items()}
        apps = [SelectOptionDict(value=APPS_NEW_ID, label="Add new")] + [
            SelectOptionDict(value=k, label=v) for k, v in apps_list.items()
        ]
        rules = [RULES_NEW_ID, *self._state_det_rules]
        options = self.config_entry.options

        data_schema = probatio.Schema(
            {
                probatio.Optional(CONF_APPS): SelectSelector(
                    SelectSelectorConfig(options=apps, mode=SelectSelectorMode.DROPDOWN)
                ),
                probatio.Optional(
                    CONF_GET_SOURCES,
                    default=options.get(CONF_GET_SOURCES, DEFAULT_GET_SOURCES),
                ): bool,
                probatio.Optional(
                    CONF_EXCLUDE_UNNAMED_APPS,
                    default=options.get(
                        CONF_EXCLUDE_UNNAMED_APPS, DEFAULT_EXCLUDE_UNNAMED_APPS
                    ),
                ): bool,
                probatio.Required(
                    CONF_SCREENCAP_INTERVAL,
                    default=options.get(
                        CONF_SCREENCAP_INTERVAL, DEFAULT_SCREENCAP_INTERVAL
                    ),
                ): probatio.All(probatio.Coerce(int), probatio.Clamp(min=0, max=15)),
                probatio.Optional(
                    CONF_TURN_OFF_COMMAND,
                    description={
                        "suggested_value": options.get(CONF_TURN_OFF_COMMAND, "")
                    },
                ): str,
                probatio.Optional(
                    CONF_TURN_ON_COMMAND,
                    description={
                        "suggested_value": options.get(CONF_TURN_ON_COMMAND, "")
                    },
                ): str,
                probatio.Optional(CONF_STATE_DETECTION_RULES): SelectSelector(
                    SelectSelectorConfig(
                        options=rules, mode=SelectSelectorMode.DROPDOWN
                    )
                ),
                probatio.Optional(
                    CONF_RELAUNCH_HELPER,
                    default=options.get(CONF_RELAUNCH_HELPER, DEFAULT_RELAUNCH_HELPER),
                ): bool,
                probatio.Optional(
                    CONF_RELAUNCH_ENTITY,
                    description={"suggested_value": options.get(CONF_RELAUNCH_ENTITY)},
                ): EntitySelector(EntitySelectorConfig(domain="remote")),
                probatio.Optional(
                    CONF_KEEP_PAIRING,
                    default=options.get(CONF_KEEP_PAIRING, DEFAULT_KEEP_PAIRING),
                ): bool,
            }
        )

        return self.async_show_form(step_id="init", data_schema=data_schema)

    async def async_step_apps(
        self, user_input: dict[str, Any] | None = None, app_id: str | None = None
    ) -> ConfigFlowResult:
        """Handle options flow for apps list."""
        if app_id is not None:
            self._conf_app_id = app_id if app_id != APPS_NEW_ID else None
            return self._async_apps_form(app_id)

        if user_input is not None:
            app_id = user_input.get(CONF_APP_ID, self._conf_app_id)
            if app_id:
                if user_input.get(CONF_APP_DELETE, False):
                    self._apps.pop(app_id)
                else:
                    self._apps[app_id] = user_input.get(CONF_APP_NAME, "")

        return await self.async_step_init()

    @callback
    def _async_apps_form(self, app_id: str) -> ConfigFlowResult:
        """Return configuration form for apps."""
        app_schema = {
            probatio.Optional(
                CONF_APP_NAME,
                description={"suggested_value": self._apps.get(app_id, "")},
            ): str,
        }
        if app_id == APPS_NEW_ID:
            data_schema = probatio.Schema(
                {**app_schema, probatio.Optional(CONF_APP_ID): str}
            )
        else:
            data_schema = probatio.Schema(
                {**app_schema, probatio.Optional(CONF_APP_DELETE, default=False): bool}
            )

        return self.async_show_form(
            step_id="apps",
            data_schema=data_schema,
            description_placeholders={
                "app_id": f"`{app_id}`" if app_id != APPS_NEW_ID else "",
            },
        )

    async def async_step_rules(
        self, user_input: dict[str, Any] | None = None, rule_id: str | None = None
    ) -> ConfigFlowResult:
        """Handle options flow for detection rules."""
        if rule_id is not None:
            self._conf_rule_id = rule_id if rule_id != RULES_NEW_ID else None
            return self._async_rules_form(rule_id)

        if user_input is not None:
            rule_id = user_input.get(CONF_RULE_ID, self._conf_rule_id)
            if rule_id:
                if user_input.get(CONF_RULE_DELETE, False):
                    self._state_det_rules.pop(rule_id)
                elif det_rule := user_input.get(CONF_RULE_VALUES):
                    state_det_rule = _validate_state_det_rules(det_rule)
                    if state_det_rule is None:
                        return self._async_rules_form(
                            rule_id=self._conf_rule_id or RULES_NEW_ID,
                            default_id=rule_id,
                            errors={"base": "invalid_det_rules"},
                        )
                    self._state_det_rules[rule_id] = state_det_rule

        return await self.async_step_init()

    @callback
    def _async_rules_form(
        self, rule_id: str, default_id: str = "", errors: dict[str, str] | None = None
    ) -> ConfigFlowResult:
        """Return configuration form for detection rules."""
        rule_schema = {
            probatio.Optional(
                CONF_RULE_VALUES, default=self._state_det_rules.get(rule_id)
            ): ObjectSelector()
        }
        if rule_id == RULES_NEW_ID:
            data_schema = probatio.Schema(
                {
                    probatio.Optional(CONF_RULE_ID, default=default_id): str,
                    **rule_schema,
                }
            )
        else:
            data_schema = probatio.Schema(
                {
                    **rule_schema,
                    probatio.Optional(CONF_RULE_DELETE, default=False): bool,
                }
            )

        return self.async_show_form(
            step_id="rules",
            data_schema=data_schema,
            description_placeholders={
                "rule_id": f"`{rule_id}`" if rule_id != RULES_NEW_ID else "",
            },
            errors=errors,
        )


def _validate_state_det_rules(state_det_rules: Any) -> list[Any] | None:
    """Validate a string that contain state detection rules and return a dict."""
    json_rules = state_det_rules
    if not isinstance(json_rules, list):
        json_rules = [json_rules]

    try:
        state_detection_rules_validator(json_rules, ValueError)
    except ValueError as exc:
        _LOGGER.warning("Invalid state detection rules: %s", exc)
        return None
    return json_rules
