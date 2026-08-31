#include "ble_client.h"

#include "payload_parser.h"

#include "esp_log.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "host/ble_gap.h"
#include "host/ble_gatt.h"
#include "host/ble_hs.h"
#include "host/ble_store.h"
#include "host/ble_uuid.h"
#include "nimble/nimble_port.h"
#include "nimble/nimble_port_freertos.h"
#include "services/gap/ble_svc_gap.h"
#include "host/util/util.h"


#include <cinttypes>
#include <cstdio>
#include <cstring>
#include <sys/select.h>
#include <unistd.h>

static const char *TAG = "ble_client";

extern "C" void ble_store_config_init(void);

static app_config_t s_cfg;
static ble_notify_cb_t s_notify_cb = nullptr;
static void (*s_connected_cb)(bool) = nullptr;

static uint16_t s_conn_handle = BLE_HS_CONN_HANDLE_NONE;
static bool s_ble_connected = false;
static bool s_should_run = true;

static ble_addr_t s_target_addr = {};
static bool s_target_addr_valid = false;

enum class BlePhase {
    Idle,
    Scanning,
    Connecting,
    Discovering,
    Subscribing,
    Connected,
    WaitingReconnect,
};

static BlePhase s_phase = BlePhase::Idle;
static size_t s_subscribe_index = 0;
static int s_pending_cccd_writes = 0;
static bool s_gatt_discovery_started = false;
static bool s_passkey_task_active = false;
static bool s_passkey_seen_this_session = false;
static bool s_security_initiate_attempted = false;
static bool s_link_encrypted = false;
static int s_discovered_chr_count = 0;
static TickType_t s_connect_tick = 0;
static TickType_t s_last_read_poll_tick = 0;
static bool s_read_in_flight = false;
static size_t s_read_poll_index = 0;
static bool s_target_seen_in_scan = false;
static bool s_use_passive_scan = false;

static constexpr uint32_t READ_POLL_INTERVAL_MS = 2000;
static constexpr uint32_t CONNECT_TIMEOUT_MS = 20000;
static constexpr uint32_t RECONNECT_TASK_IDLE_MS = 1000;

struct PasskeyPrompt {
    uint16_t conn_handle;
    uint8_t action;
    uint32_t numcmp;
};

static void begin_secure_session(void);
static void start_gatt_discovery(void);

static uint32_t read_passkey_from_console(int timeout_sec) {
    ESP_LOGI(TAG, ">>> Введите код с дисплея датчика (6 цифр) и нажмите Enter (до %d сек):", timeout_sec);
    char buf[16] = {0};
    if (fileno(stdin) < 0) {
        ESP_LOGE(TAG, "Serial console недоступен — задайте ble_passkey в config.json");
        return 0;
    }
    fd_set rfds;
    FD_ZERO(&rfds);
    FD_SET(fileno(stdin), &rfds);
    struct timeval tv = {
        .tv_sec = timeout_sec,
        .tv_usec = 0,
    };
    if (select(fileno(stdin) + 1, &rfds, nullptr, nullptr, &tv) <= 0) {
        ESP_LOGE(TAG, "Timeout ожидания passkey");
        return 0;
    }
    if (fgets(buf, sizeof(buf), stdin) == nullptr) {
        return 0;
    }
    return static_cast<uint32_t>(strtoul(buf, nullptr, 10));
}

static void passkey_worker_task(void *param) {
    auto *prompt = static_cast<PasskeyPrompt *>(param);
    struct ble_sm_io pkey = {};

    switch (prompt->action) {
    case BLE_SM_IOACT_INPUT: {
        uint32_t passkey = s_cfg.sensor.ble_passkey;
        if (passkey == 0) {
            passkey = read_passkey_from_console(120);
        }
        if (passkey == 0) {
            ESP_LOGE(TAG, "Passkey не задан — pairing отменён");
            break;
        }
        pkey.action = BLE_SM_IOACT_INPUT;
        pkey.passkey = passkey;
        ESP_LOGI(TAG, "Отправка passkey %06" PRIu32, passkey);
        ble_sm_inject_io(prompt->conn_handle, &pkey);
        break;
    }
    case BLE_SM_IOACT_DISP:
        ESP_LOGI(TAG, "Введите на датчике код %06" PRIu32 " (кнопками на START-STOP)", prompt->numcmp);
        pkey.action = BLE_SM_IOACT_DISP;
        pkey.passkey = prompt->numcmp;
        ble_sm_inject_io(prompt->conn_handle, &pkey);
        break;
    case BLE_SM_IOACT_NUMCMP:
        ESP_LOGI(TAG, "Подтвердите на датчике код %06" PRIu32 " (если совпадает с дисплеем)", prompt->numcmp);
        pkey.action = BLE_SM_IOACT_NUMCMP;
        pkey.numcmp_accept = 1;
        ble_sm_inject_io(prompt->conn_handle, &pkey);
        break;
    default:
        ESP_LOGW(TAG, "Unknown passkey action %u", prompt->action);
        break;
    }

    delete prompt;
    s_passkey_task_active = false;
    vTaskDelete(nullptr);
}

static void queue_passkey_action(uint16_t conn_handle, uint8_t action, uint32_t numcmp) {
    if (s_passkey_task_active) {
        ESP_LOGW(TAG, "Passkey task already running");
        return;
    }
    auto *prompt = new PasskeyPrompt{conn_handle, action, numcmp};
    s_passkey_task_active = true;
    xTaskCreate(passkey_worker_task, "ble_passkey", 4096, prompt, 5, nullptr);
}

static bool is_link_encrypted(uint16_t conn_handle) {
    struct ble_gap_conn_desc desc;
    if (ble_gap_conn_find(conn_handle, &desc) != 0) {
        return false;
    }
    return desc.sec_state.encrypted != 0;
}

static int ble_gap_event(struct ble_gap_event *event, void *arg);
static int on_gatt_disc_chr(uint16_t conn_handle, const struct ble_gatt_error *error, const struct ble_gatt_chr *chr,
                            void *arg);
static int on_gatt_write_cccd(uint16_t conn_handle, const struct ble_gatt_error *error, struct ble_gatt_attr *attr,
                              void *arg);
static void schedule_reconnect(void);
static int discover_characteristics(void);
static int start_scan(void);
static int connect_to_target(void);
static int discover_characteristics(void);
static int subscribe_next(void);

static bool char_supports_notify(const char_config_t *ch) {
    return ch != nullptr &&
           (ch->properties & (BLE_GATT_CHR_F_NOTIFY | BLE_GATT_CHR_F_INDICATE)) != 0;
}

static bool char_supports_read(const char_config_t *ch) {
    return ch != nullptr && (ch->properties & BLE_GATT_CHR_F_READ) != 0;
}

static void publish_char_value(char_config_t *ch, const uint8_t *data, size_t len) {
    if (ch == nullptr || s_notify_cb == nullptr || data == nullptr || len == 0) {
        return;
    }
    char payload[32];
    if (ch->type == CHAR_TYPE_TEMPERATURE) {
        float temp = 0.0f;
        if (!parse_temperature(data, len, &temp)) {
            ESP_LOGW(TAG, "Bad temperature payload uuid=%s len=%u", ch->uuid, len);
            return;
        }
        std::snprintf(payload, sizeof(payload), "%.2f", temp);
    } else {
        bool value = false;
        if (!parse_boolean(data, len, &value)) {
            ESP_LOGW(TAG, "Bad boolean payload uuid=%s len=%u", ch->uuid, len);
            return;
        }
        std::snprintf(payload, sizeof(payload), "%s", value ? "true" : "false");
    }
    s_notify_cb(ch, payload);
}

static int on_gatt_read(uint16_t conn_handle, const struct ble_gatt_error *error, struct ble_gatt_attr *attr,
                        void *arg) {
    s_read_in_flight = false;
    auto *ch = static_cast<char_config_t *>(arg);
    if (error->status != 0) {
        ESP_LOGW(TAG, "GATT read failed uuid=%s status=%d", ch != nullptr ? ch->uuid : "?", error->status);
        return 0;
    }
    if (ch == nullptr || attr->om == nullptr) {
        return 0;
    }
    publish_char_value(ch, attr->om->om_data, attr->om->om_len);
    return 0;
}

static void poll_read_characteristics(void) {
    if (s_read_in_flight || s_conn_handle == BLE_HS_CONN_HANDLE_NONE || s_phase != BlePhase::Connected) {
        return;
    }
    if (s_cfg.sensor.characteristic_count == 0) {
        return;
    }

    for (size_t n = 0; n < s_cfg.sensor.characteristic_count; ++n) {
        size_t i = (s_read_poll_index + n) % s_cfg.sensor.characteristic_count;
        char_config_t *ch = &s_cfg.sensor.characteristics[i];
        if (ch->val_handle == 0 || char_supports_notify(ch) || !char_supports_read(ch)) {
            continue;
        }
        s_read_poll_index = (i + 1) % s_cfg.sensor.characteristic_count;
        s_read_in_flight = true;
        int rc = ble_gattc_read(s_conn_handle, ch->val_handle, on_gatt_read, ch);
        if (rc != 0) {
            s_read_in_flight = false;
            ESP_LOGW(TAG, "ble_gattc_read failed uuid=%s rc=%d", ch->uuid, rc);
        }
        return;
    }
}

static bool parse_mac(const char *mac_str, ble_addr_t *addr) {
    if (mac_str == nullptr || addr == nullptr) {
        return false;
    }
    unsigned int bytes[6];
    if (std::sscanf(mac_str, "%x:%x:%x:%x:%x:%x", &bytes[0], &bytes[1], &bytes[2], &bytes[3], &bytes[4],
                    &bytes[5]) != 6) {
        return false;
    }
    addr->type = BLE_ADDR_PUBLIC;
    for (int i = 0; i < 6; ++i) {
        addr->val[5 - i] = static_cast<uint8_t>(bytes[i]);
    }
    return true;
}

static char_config_t *find_char_by_handle(uint16_t handle) {
    for (size_t i = 0; i < s_cfg.sensor.characteristic_count; ++i) {
        if (s_cfg.sensor.characteristics[i].val_handle == handle) {
            return &s_cfg.sensor.characteristics[i];
        }
    }
    return nullptr;
}

static void reset_handles(void) {
    for (size_t i = 0; i < s_cfg.sensor.characteristic_count; ++i) {
        s_cfg.sensor.characteristics[i].val_handle = 0;
        s_cfg.sensor.characteristics[i].cccd_handle = 0;
        s_cfg.sensor.characteristics[i].properties = 0;
        s_cfg.sensor.characteristics[i].subscribed = false;
    }
}

static void set_ble_connected(bool connected) {
    if (s_ble_connected == connected) {
        return;
    }
    s_ble_connected = connected;
    if (s_connected_cb != nullptr) {
        s_connected_cb(connected);
    }
}

static void schedule_reconnect(void) {
    if (s_phase == BlePhase::WaitingReconnect) {
        return;
    }
    if (s_conn_handle != BLE_HS_CONN_HANDLE_NONE) {
        ble_gap_terminate(s_conn_handle, BLE_ERR_REM_USER_CONN_TERM);
        s_conn_handle = BLE_HS_CONN_HANDLE_NONE;
    }
    s_gatt_discovery_started = false;
    s_link_encrypted = false;
    s_read_in_flight = false;
    s_target_seen_in_scan = false;
    reset_handles();
    set_ble_connected(false);
    s_use_passive_scan = true;
    s_phase = BlePhase::WaitingReconnect;
    ESP_LOGI(TAG, "Sensor unavailable, next probe in %" PRIu32 "s (scan %" PRIu32 "s)",
             s_cfg.sensor.reconnect_interval_sec, s_cfg.sensor.reconnect_scan_timeout_sec);
}

static int start_scan(void) {
    struct ble_gap_disc_params params = {};
    params.filter_duplicates = 1;
    params.passive = s_use_passive_scan ? 1 : 0;
    s_target_seen_in_scan = false;
    s_phase = BlePhase::Scanning;
    ESP_LOGI(TAG, "Scanning for %s (%" PRIu32 "s, %s)", s_cfg.sensor.mac_address,
             s_cfg.sensor.reconnect_scan_timeout_sec, s_use_passive_scan ? "passive" : "active");
    return ble_gap_disc(BLE_OWN_ADDR_PUBLIC, s_cfg.sensor.reconnect_scan_timeout_sec * 1000, &params, ble_gap_event,
                        nullptr);
}

static int connect_to_target(void) {
    if (!s_target_addr_valid) {
        return BLE_HS_EINVAL;
    }
    s_phase = BlePhase::Connecting;
    struct ble_gap_conn_params params = {};
    params.scan_itvl = 0x0010;
    params.scan_window = 0x0010;
    params.itvl_min = BLE_GAP_INITIAL_CONN_ITVL_MIN;
    params.itvl_max = BLE_GAP_INITIAL_CONN_ITVL_MAX;
    params.supervision_timeout = 400;
    ESP_LOGI(TAG, "Connecting");
    return ble_gap_connect(BLE_OWN_ADDR_PUBLIC, &s_target_addr, CONNECT_TIMEOUT_MS, &params, ble_gap_event, nullptr);
}


static void start_gatt_discovery(void) {
    if (s_gatt_discovery_started || s_conn_handle == BLE_HS_CONN_HANDLE_NONE) {
        return;
    }
    s_gatt_discovery_started = true;
    discover_characteristics();
}

static void begin_secure_session(void) {
    if (s_conn_handle == BLE_HS_CONN_HANDLE_NONE || s_security_initiate_attempted) {
        return;
    }
    if (is_link_encrypted(s_conn_handle)) {
        ESP_LOGI(TAG, "Link already encrypted");
        s_link_encrypted = true;
        start_gatt_discovery();
        return;
    }
    s_security_initiate_attempted = true;
    ESP_LOGI(TAG, "Central-initiated pairing/encryption");
    int rc = ble_gap_security_initiate(s_conn_handle);
    if (rc == 0) {
        return;
    }
    if (rc == BLE_HS_EALREADY || rc == BLE_HS_EBUSY) {
        ESP_LOGI(TAG, "Security procedure already active (rc=%d)", rc);
        return;
    }
    ESP_LOGE(TAG, "ble_gap_security_initiate failed: %d", rc);
    schedule_reconnect();
}

static int discover_characteristics(void) {
    s_phase = BlePhase::Discovering;
    s_discovered_chr_count = 0;
    ESP_LOGI(TAG, "GATT characteristic discovery...");
    return ble_gattc_disc_all_chrs(s_conn_handle, 1, 65535, on_gatt_disc_chr, nullptr);
}

static int subscribe_next(void) {
    while (s_subscribe_index < s_cfg.sensor.characteristic_count) {
        char_config_t *ch = &s_cfg.sensor.characteristics[s_subscribe_index++];
        if (ch->val_handle == 0) {
            ESP_LOGW(TAG, "Skip subscribe (no handles): %s", ch->uuid);
            continue;
        }
        if (!char_supports_notify(ch)) {
            ESP_LOGI(TAG, "Read/poll %s val=%u props=0x%02x", ch->uuid, ch->val_handle, ch->properties);
            continue;
        }
        if (ch->cccd_handle == 0) {
            ESP_LOGW(TAG, "Skip subscribe (no CCCD): %s", ch->uuid);
            continue;
        }
        if (ch->subscribed) {
            continue;
        }
        uint16_t value = 1;
        s_phase = BlePhase::Subscribing;
        s_pending_cccd_writes++;
        ESP_LOGI(TAG, "Subscribe %s val=%u cccd=%u", ch->uuid, ch->val_handle, ch->cccd_handle);
        return ble_gattc_write_flat(s_conn_handle, ch->cccd_handle, &value, sizeof(value), on_gatt_write_cccd, ch);
    }

    s_phase = BlePhase::Connected;
    set_ble_connected(true);
    s_last_read_poll_tick = 0;
    s_read_poll_index = 0;
    ESP_LOGI(TAG, "GATT notify setup finished");
    poll_read_characteristics();
    return 0;
}

static int on_gatt_write_cccd(uint16_t conn_handle, const struct ble_gatt_error *error, struct ble_gatt_attr *attr,
                              void *arg) {
    char_config_t *ch = static_cast<char_config_t *>(arg);
    if (error->status != 0) {
        ESP_LOGE(TAG, "CCCD write failed status=%d uuid=%s", error->status, ch != nullptr ? ch->uuid : "?");
    } else if (ch != nullptr) {
        ch->subscribed = true;
    }
    if (s_pending_cccd_writes > 0) {
        s_pending_cccd_writes--;
    }
    if (s_pending_cccd_writes == 0) {
        subscribe_next();
    }
    return 0;
}

static int on_gatt_disc_chr(uint16_t conn_handle, const struct ble_gatt_error *error, const struct ble_gatt_chr *chr,
                            void *arg) {
    if (error->status == BLE_HS_EDONE) {
        ESP_LOGI(TAG, "GATT discovery done, characteristics seen=%d", s_discovered_chr_count);
        for (size_t i = 0; i < s_cfg.sensor.characteristic_count; ++i) {
            if (s_cfg.sensor.characteristics[i].val_handle == 0) {
                ESP_LOGW(TAG, "Missing configured UUID: %s", s_cfg.sensor.characteristics[i].uuid);
            }
        }
        s_subscribe_index = 0;
        s_pending_cccd_writes = 0;
        return subscribe_next();
    }
    if (error->status != 0) {
        ESP_LOGE(TAG, "Characteristic discovery failed: %d", error->status);
        schedule_reconnect();
        return 0;
    }

    s_discovered_chr_count++;
    char uuid_buf[BLE_UUID_STR_LEN];
    ble_uuid_to_str(&chr->uuid.u, uuid_buf);
    ESP_LOGI(TAG, "GATT char: %s val_handle=%u", uuid_buf, chr->val_handle);

    for (size_t i = 0; i < s_cfg.sensor.characteristic_count; ++i) {
        char_config_t *cfg_ch = &s_cfg.sensor.characteristics[i];
        if (cfg_ch->val_handle != 0) {
            continue;
        }
        ble_uuid_any_t wanted = {};
        if (ble_uuid_from_str(&wanted, cfg_ch->uuid) != 0) {
            continue;
        }
        if (ble_uuid_cmp(&chr->uuid.u, &wanted.u) == 0) {
            cfg_ch->val_handle = chr->val_handle;
            cfg_ch->properties = chr->properties;
            cfg_ch->cccd_handle =
                char_supports_notify(cfg_ch) ? static_cast<uint16_t>(chr->val_handle + 1) : 0;
            ESP_LOGI(TAG, "Matched config UUID %s val=%u cccd=%u props=0x%02x", cfg_ch->uuid, cfg_ch->val_handle,
                     cfg_ch->cccd_handle, cfg_ch->properties);
            break;
        }
    }
    return 0;
}

static int ble_gap_event(struct ble_gap_event *event, void *arg) {
    switch (event->type) {
    case BLE_GAP_EVENT_DISC:
        if (s_phase != BlePhase::Scanning) {
            return 0;
        }
        if (event->disc.addr.type == s_target_addr.type &&
            std::memcmp(event->disc.addr.val, s_target_addr.val, 6) == 0) {
            ESP_LOGI(TAG, "Target found rssi=%d", event->disc.rssi);
            ble_gap_disc_cancel();
            s_target_addr = event->disc.addr;
            s_target_addr_valid = true;
            s_target_seen_in_scan = true;
            return connect_to_target();
        }
        return 0;
    case BLE_GAP_EVENT_DISC_COMPLETE:
        if (s_phase == BlePhase::Scanning) {
            if (s_target_seen_in_scan) {
                return connect_to_target();
            }
            ESP_LOGI(TAG, "Sensor not advertising");
            schedule_reconnect();
        }
        return 0;
    case BLE_GAP_EVENT_CONNECT:
        if (event->connect.status == 0) {
            s_conn_handle = event->connect.conn_handle;
            s_gatt_discovery_started = false;
            s_link_encrypted = false;
            s_passkey_seen_this_session = false;
            s_security_initiate_attempted = false;
            s_connect_tick = xTaskGetTickCount();
            ESP_LOGI(TAG, "Connected handle=%u", s_conn_handle);
            if (is_link_encrypted(s_conn_handle)) {
                s_link_encrypted = true;
                ESP_LOGI(TAG, "Bonded link already encrypted");
                start_gatt_discovery();
            }
        } else {
            ESP_LOGW(TAG, "Connect failed status=%d", event->connect.status);
            schedule_reconnect();
        }
        return 0;
    case BLE_GAP_EVENT_DISCONNECT:
        ESP_LOGW(TAG, "Disconnected reason=%d", event->disconnect.reason);
        s_conn_handle = BLE_HS_CONN_HANDLE_NONE;
        schedule_reconnect();
        return 0;
    case BLE_GAP_EVENT_NOTIFY_RX: {
        char_config_t *ch = find_char_by_handle(event->notify_rx.attr_handle);
        if (ch == nullptr) {
            ESP_LOGW(TAG, "Notify on unknown handle=%u len=%u", event->notify_rx.attr_handle,
                     event->notify_rx.om->om_len);
            return 0;
        }
        if (s_notify_cb == nullptr) {
            return 0;
        }
        publish_char_value(ch, event->notify_rx.om->om_data, event->notify_rx.om->om_len);
        return 0;
    }
    case BLE_GAP_EVENT_ENC_CHANGE:
        if (event->enc_change.status == 0) {
            if (s_link_encrypted) {
                ESP_LOGD(TAG, "Encryption refreshed");
                return 0;
            }
            s_link_encrypted = true;
            ESP_LOGI(TAG, "Encryption established");
            start_gatt_discovery();
        } else {
            ESP_LOGE(TAG, "Encryption failed status=%d (check ble_passkey in config)", event->enc_change.status);
            schedule_reconnect();
        }
        return 0;
    case BLE_GAP_EVENT_PASSKEY_ACTION:
        s_passkey_seen_this_session = true;
        ESP_LOGI(TAG, "Passkey action=%u", event->passkey.params.action);
        queue_passkey_action(event->passkey.conn_handle, event->passkey.params.action,
                             event->passkey.params.numcmp);
        return 0;
    default:
        return 0;
    }
}

static void on_ble_sync(void) {
    if (!parse_mac(s_cfg.sensor.mac_address, &s_target_addr)) {
        ESP_LOGE(TAG, "Invalid MAC: %s", s_cfg.sensor.mac_address);
        return;
    }
    s_target_addr_valid = true;
    s_use_passive_scan = false;
    ble_svc_gap_device_name_set("wein-ble-mqtt");
    if (start_scan() != 0) {
        ESP_LOGW(TAG, "Initial scan failed");
        schedule_reconnect();
    }
}

static void ble_host_task(void *param) {
    nimble_port_run();
    nimble_port_freertos_deinit();
}

static void reconnect_task(void *param) {
    while (s_should_run) {
        if (s_phase == BlePhase::WaitingReconnect) {
            vTaskDelay(pdMS_TO_TICKS(s_cfg.sensor.reconnect_interval_sec * 1000));
            if (s_phase != BlePhase::WaitingReconnect) {
                continue;
            }
            if (start_scan() != 0) {
                ESP_LOGW(TAG, "Scan start failed");
                schedule_reconnect();
            }
        } else if (s_conn_handle != BLE_HS_CONN_HANDLE_NONE && !s_link_encrypted && !s_gatt_discovery_started) {
            TickType_t elapsed = xTaskGetTickCount() - s_connect_tick;
            if (s_passkey_seen_this_session || s_passkey_task_active) {
                if (elapsed > pdMS_TO_TICKS(60000)) {
                    ESP_LOGE(TAG, "Pairing timeout");
                    schedule_reconnect();
                }
            } else if (elapsed > pdMS_TO_TICKS(8000) && !s_security_initiate_attempted) {
                begin_secure_session();
            }
            vTaskDelay(pdMS_TO_TICKS(500));
        } else if (s_phase == BlePhase::Connected && s_conn_handle != BLE_HS_CONN_HANDLE_NONE) {
            TickType_t now = xTaskGetTickCount();
            if (s_last_read_poll_tick == 0 ||
                (now - s_last_read_poll_tick) >= pdMS_TO_TICKS(READ_POLL_INTERVAL_MS)) {
                s_last_read_poll_tick = now;
                poll_read_characteristics();
            }
            vTaskDelay(pdMS_TO_TICKS(RECONNECT_TASK_IDLE_MS));
        } else {
            vTaskDelay(pdMS_TO_TICKS(500));
        }
    }
}

bool ble_client_init(const app_config_t *cfg, ble_notify_cb_t on_notify) {
    if (cfg == nullptr || on_notify == nullptr) {
        return false;
    }
    s_cfg = *cfg;
    s_notify_cb = on_notify;

    esp_err_t err = nimble_port_init();
    if (err != ESP_OK) {
        ESP_LOGE(TAG, "NimBLE init failed: %s", esp_err_to_name(err));
        return false;
    }

    ble_hs_cfg.sync_cb = on_ble_sync;
    ble_hs_cfg.sm_io_cap = BLE_HS_IO_KEYBOARD_DISPLAY;
    ble_hs_cfg.sm_bonding = 1;
    ble_hs_cfg.sm_mitm = 1;
    ble_hs_cfg.sm_sc = 1;
    ble_store_config_init();
    ble_svc_gap_init();

    nimble_port_freertos_init(ble_host_task);
    xTaskCreate(reconnect_task, "ble_reconnect", 4096, nullptr, 5, nullptr);
    return true;
}

void ble_client_start(void) {}

void ble_client_set_connected_cb(void (*cb)(bool connected)) {
    s_connected_cb = cb;
}

bool ble_client_is_connected(void) {
    return s_ble_connected;
}
