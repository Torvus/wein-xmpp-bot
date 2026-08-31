#include "app_config.h"

#include "cJSON.h"
#include "esp_log.h"
#include "esp_spiffs.h"

#include <cctype>
#include <cstdio>
#include <cstring>

static const char *TAG = "app_config";
static const char *CONFIG_PATH = "/config/config.json";

static void copy_string(char *dest, size_t dest_size, const char *src) {
    if (src == nullptr) {
        dest[0] = '\0';
        return;
    }
    std::snprintf(dest, dest_size, "%s", src);
}

static char_type_t parse_char_type(const char *type) {
    if (type != nullptr && std::strcmp(type, "temperature") == 0) {
        return CHAR_TYPE_TEMPERATURE;
    }
    return CHAR_TYPE_BOOLEAN;
}

const char *char_type_to_string(char_type_t type) {
    return type == CHAR_TYPE_TEMPERATURE ? "temperature" : "boolean";
}

static bool mount_spiffs(void) {
    esp_vfs_spiffs_conf_t conf = {
        .base_path = "/config",
        .partition_label = "littlefs",
        .max_files = 4,
        .format_if_mount_failed = false,
    };
    esp_err_t err = esp_vfs_spiffs_register(&conf);
    if (err == ESP_ERR_INVALID_STATE) {
        return true;
    }
    if (err != ESP_OK) {
        ESP_LOGE(TAG, "SPIFFS mount failed: %s", esp_err_to_name(err));
        return false;
    }
    return true;
}

static void make_device_id(const char *mac, char *device_id, size_t size) {
    size_t j = 0;
    for (size_t i = 0; mac[i] != '\0' && j + 1 < size; ++i) {
        if (mac[i] == ':' || mac[i] == '-') {
            continue;
        }
        device_id[j++] = static_cast<char>(std::tolower(static_cast<unsigned char>(mac[i])));
    }
    device_id[j] = '\0';
}

static bool parse_characteristics(cJSON *array, sensor_config_t *sensor) {
    if (!cJSON_IsArray(array)) {
        return false;
    }
    sensor->characteristic_count = 0;
    const cJSON *item = nullptr;
    cJSON_ArrayForEach(item, array) {
        if (sensor->characteristic_count >= APP_MAX_CHARS) {
            ESP_LOGW(TAG, "Too many characteristics, max %d", APP_MAX_CHARS);
            break;
        }
        char_config_t *ch = &sensor->characteristics[sensor->characteristic_count];
        ch->val_handle = 0;
        ch->cccd_handle = 0;
        ch->subscribed = false;

        copy_string(ch->uuid, sizeof(ch->uuid), cJSON_GetStringValue(cJSON_GetObjectItem(item, "uuid")));
        copy_string(ch->topic, sizeof(ch->topic), cJSON_GetStringValue(cJSON_GetObjectItem(item, "topic")));
        ch->type = parse_char_type(cJSON_GetStringValue(cJSON_GetObjectItem(item, "type")));
        if (ch->uuid[0] == '\0' || ch->topic[0] == '\0') {
            ESP_LOGW(TAG, "Skipping characteristic with empty uuid/topic");
            continue;
        }
        sensor->characteristic_count++;
    }
    return sensor->characteristic_count > 0;
}

bool app_config_load(app_config_t *cfg) {
    if (cfg == nullptr) {
        return false;
    }
    std::memset(cfg, 0, sizeof(*cfg));
    copy_string(cfg->mqtt.discovery_prefix, sizeof(cfg->mqtt.discovery_prefix), "homeassistant");
    copy_string(cfg->mqtt.protocol, sizeof(cfg->mqtt.protocol), "3.1.1");
    cfg->mqtt.port = 1883;
    cfg->sensor.reconnect_interval_sec = 600;
    cfg->sensor.reconnect_scan_timeout_sec = 15;

    if (!mount_spiffs()) {
        return false;
    }

    FILE *file = std::fopen(CONFIG_PATH, "r");
    if (file == nullptr) {
        ESP_LOGE(TAG, "Cannot open %s — copy data/config.example.json to data/config.json and run: pio run -t uploadfs", CONFIG_PATH);
        return false;
    }

    std::fseek(file, 0, SEEK_END);
    long file_size = std::ftell(file);
    std::rewind(file);
    if (file_size <= 0 || file_size > 8192) {
        std::fclose(file);
        ESP_LOGE(TAG, "Invalid config size: %ld", file_size);
        return false;
    }

    char *json_text = static_cast<char *>(std::malloc(static_cast<size_t>(file_size) + 1));
    if (json_text == nullptr) {
        std::fclose(file);
        return false;
    }
    size_t read = std::fread(json_text, 1, static_cast<size_t>(file_size), file);
    std::fclose(file);
    json_text[read] = '\0';

    cJSON *root = cJSON_Parse(json_text);
    std::free(json_text);
    if (root == nullptr) {
        ESP_LOGE(TAG, "JSON parse error");
        return false;
    }

    cJSON *wifi = cJSON_GetObjectItem(root, "wifi");
    cJSON *mqtt = cJSON_GetObjectItem(root, "mqtt");
    cJSON *sensor = cJSON_GetObjectItem(root, "sensor");

    if (!cJSON_IsObject(wifi) || !cJSON_IsObject(mqtt) || !cJSON_IsObject(sensor)) {
        ESP_LOGE(TAG, "Missing wifi/mqtt/sensor sections");
        cJSON_Delete(root);
        return false;
    }

    copy_string(cfg->wifi.ssid, sizeof(cfg->wifi.ssid), cJSON_GetStringValue(cJSON_GetObjectItem(wifi, "ssid")));
    copy_string(cfg->wifi.password, sizeof(cfg->wifi.password), cJSON_GetStringValue(cJSON_GetObjectItem(wifi, "password")));

    copy_string(cfg->mqtt.host, sizeof(cfg->mqtt.host), cJSON_GetStringValue(cJSON_GetObjectItem(mqtt, "host")));
    copy_string(cfg->mqtt.username, sizeof(cfg->mqtt.username), cJSON_GetStringValue(cJSON_GetObjectItem(mqtt, "username")));
    copy_string(cfg->mqtt.password, sizeof(cfg->mqtt.password), cJSON_GetStringValue(cJSON_GetObjectItem(mqtt, "password")));
    copy_string(cfg->mqtt.discovery_prefix, sizeof(cfg->mqtt.discovery_prefix),
                cJSON_GetStringValue(cJSON_GetObjectItem(mqtt, "discovery_prefix")));
    copy_string(cfg->mqtt.protocol, sizeof(cfg->mqtt.protocol),
                cJSON_GetStringValue(cJSON_GetObjectItem(mqtt, "protocol")));
    if (cfg->mqtt.protocol[0] == '\0') {
        copy_string(cfg->mqtt.protocol, sizeof(cfg->mqtt.protocol), "3.1.1");
    }
    cfg->mqtt.port = static_cast<uint16_t>(cJSON_GetNumberValue(cJSON_GetObjectItem(mqtt, "port")));
    if (cfg->mqtt.port == 0) {
        cfg->mqtt.port = 1883;
    }
    cfg->mqtt.discovery = cJSON_IsTrue(cJSON_GetObjectItem(mqtt, "discovery"));

    copy_string(cfg->sensor.mac_address, sizeof(cfg->sensor.mac_address),
                cJSON_GetStringValue(cJSON_GetObjectItem(sensor, "mac_address")));
    cfg->sensor.ble_passkey = static_cast<uint32_t>(cJSON_GetNumberValue(cJSON_GetObjectItem(sensor, "ble_passkey")));
    cfg->sensor.reconnect_interval_sec =
        static_cast<uint32_t>(cJSON_GetNumberValue(cJSON_GetObjectItem(sensor, "reconnect_interval_sec")));
    cfg->sensor.reconnect_scan_timeout_sec =
        static_cast<uint32_t>(cJSON_GetNumberValue(cJSON_GetObjectItem(sensor, "reconnect_scan_timeout_sec")));
    if (cfg->sensor.reconnect_interval_sec == 0) {
        cfg->sensor.reconnect_interval_sec = 600;
    }
    if (cfg->sensor.reconnect_scan_timeout_sec == 0) {
        cfg->sensor.reconnect_scan_timeout_sec = 15;
    }

    if (!parse_characteristics(cJSON_GetObjectItem(sensor, "characteristics"), &cfg->sensor)) {
        ESP_LOGE(TAG, "No valid characteristics configured");
        cJSON_Delete(root);
        return false;
    }

    make_device_id(cfg->sensor.mac_address, cfg->device_id, sizeof(cfg->device_id));
    cJSON_Delete(root);

    if (cfg->wifi.ssid[0] == '\0' || cfg->mqtt.host[0] == '\0' || cfg->sensor.mac_address[0] == '\0') {
        ESP_LOGE(TAG, "wifi.ssid, mqtt.host and sensor.mac_address are required");
        return false;
    }

    ESP_LOGI(TAG, "Config loaded: device_id=%s, chars=%u", cfg->device_id,
             static_cast<unsigned>(cfg->sensor.characteristic_count));
    return true;
}
