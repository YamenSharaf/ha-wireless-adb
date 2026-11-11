"""Constants for the Wireless ADB integration."""

DOMAIN = "wireless_adb"

CONF_ADBKEY = "adbkey"
CONF_APPS = "apps"
CONF_DEVICE_PROPERTIES = "device_properties"
CONF_EXCLUDE_UNNAMED_APPS = "exclude_unnamed_apps"
CONF_GET_SOURCES = "get_sources"
CONF_PAIRING_CODE = "pairing_code"
CONF_INSTALL_HELPER = "install_helper"
CONF_PAIRING_PORT = "pairing_port"
CONF_RELAUNCH_ENTITY = "relaunch_entity"
CONF_RELAUNCH_HELPER = "relaunch_helper"
CONF_SCREENCAP = "screencap"
CONF_SCREENCAP_INTERVAL = "screencap_interval"
CONF_STATE_DETECTION_RULES = "state_detection_rules"
CONF_TURN_OFF_COMMAND = "turn_off_command"
CONF_TURN_ON_COMMAND = "turn_on_command"

DEFAULT_EXCLUDE_UNNAMED_APPS = False
DEFAULT_GET_SOURCES = True
DEFAULT_RELAUNCH_HELPER = True
DEFAULT_SCREENCAP_INTERVAL = 5

# The integration's own ADB key, generated on first use and kept in .storage.
ADBKEY_FILENAME = "wireless_adb_adbkey"

# Wireless debugging advertises its (rotating) TLS port as this mDNS service.
# Instance names look like "adb-<serial>-<random>".
SERVICE_TYPE_CONNECT = "_adb-tls-connect._tcp.local."

DEVICE_ANDROIDTV = "androidtv"

# The helper app (android/ in this repo) that keeps wireless debugging on.
HELPER_PACKAGE = "dev.yamen.wirelessadb"
HELPER_LAUNCH_URI = "wirelessadb://launch"
HELPER_DIR = "helper"  # inside the integration; filled by the release workflow
HELPER_APK = "wireless-adb-helper.apk"
HELPER_INFO = "helper.json"

PROP_ETHMAC = "ethmac"
PROP_SERIALNO = "serialno"
PROP_WIFIMAC = "wifimac"

SIGNAL_CONFIG_ENTITY = "wireless_adb_config"
SIGNAL_HEALTH = "wireless_adb_health"
