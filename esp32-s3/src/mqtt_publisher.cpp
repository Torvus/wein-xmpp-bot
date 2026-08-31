#include "mqtt_publisher.h"

#include "cJSON.h"
#include "esp_event.h"
#include "esp_log.h"
#include "esp_netif.h"
#include "esp_wifi.h"
#include "mqtt_client.h"
#include "nvs_flash.h"

#include <cstdio>
#include <cstring>
#include <string>

static const char *TAG = "mqtt";
static const char *WIFI_HOSTNAME = "Moonshine-Controller";

static app_config_t s_cfg;
static esp_mqtt_client_handle_t s_client = nullptr;
static bool s_mqtt_connected = false;
static char s_mqtt_uri[128];
static char s_status_topic[64];
static char s_ble_connected_topic[64];
static mqtt_connected_cb_t s_mqtt_connected_cb = nullptr;

static void copy_field(char *dest, size_t size, const char *src) {
    std::snprintf(dest, size, "%s", src != nullptr ? src : "");
}

static cJSON *make_device_object(void) {
    cJSON *device = cJSON_CreateObject();
    if (device == nullptr) {
        return nullptr;
    }
    cJSON *identifiers = cJSON_CreateArray();
    cJSON_AddItemToArray(identifiers, cJSON_CreateString((std::string("wein_") + s_cfg.device_id).c_str()));
    cJSON_AddItemToObject(device, "identifiers", identifiers);
    cJSON_AddStringToObject(device, "name", "Wein START-STOP");
    cJSON_AddStringToObject(device, "manufacturer", "Wein");
    cJSON_AddStringToObject(device, "model", "START-STOP");
    return device;
}

static cJSON *make_gateway_availability(void) {
    cJSON *avail = cJSON_CreateArray();
    cJSON *entry = cJSON_CreateObject();
    if (avail == nullptr || entry == nullptr) {
        cJSON_Delete(avail);
        cJSON_Delete(entry);
        return nullptr;
    }
    cJSON_AddStringToObject(entry, "topic", s_status_topic);
    cJSON_AddStringToObject(entry, "payload_available", "online");
    cJSON_AddStringToObject(entry, "payload_not_available", "offline");
    cJSON_AddItemToArray(avail, entry);
    return avail;
}

static cJSON *make_sensor_availability(void) {
    cJSON *avail = make_gateway_availability();
    if (avail == nullptr) {
        return nullptr;
    }
    cJSON *entry = cJSON_CreateObject();
    if (entry == nullptr) {
        cJSON_Delete(avail);
        return nullptr;
    }
    cJSON_AddStringToObject(entry, "topic", s_ble_connected_topic);
    cJSON_AddStringToObject(entry, "payload_available", "true");
    cJSON_AddStringToObject(entry, "payload_not_available", "false");
    cJSON_AddItemToArray(avail, entry);
    return avail;
}

static void publish_discovery_json(const char *component, const char *object_id, cJSON *root) {
    if (root == nullptr || component == nullptr || object_id == nullptr) {
        cJSON_Delete(root);
        return;
    }
    char *json = cJSON_PrintUnformatted(root);
    cJSON_Delete(root);
    if (json == nullptr) {
        return;
    }
    std::string discovery_topic =
        std::string(s_cfg.mqtt.discovery_prefix) + "/" + component + "/" + object_id + "/config";
    esp_mqtt_client_publish(s_client, discovery_topic.c_str(), json, 0, 1, 1);
    cJSON_free(json);
}

static void wifi_event_handler(void *, esp_event_base_t base, int32_t event_id, void *) {
    if (base == WIFI_EVENT && event_id == WIFI_EVENT_STA_START) {
        esp_wifi_connect();
    } else if (base == WIFI_EVENT && event_id == WIFI_EVENT_STA_DISCONNECTED) {
        ESP_LOGW(TAG, "WiFi disconnected, reconnecting");
        esp_wifi_connect();
    } else if (base == IP_EVENT && event_id == IP_EVENT_STA_GOT_IP) {
        ESP_LOGI(TAG, "WiFi connected");
    }
}

static void mqtt_event_handler(void *, esp_event_base_t, int32_t event_id, void *event_data) {
    if (event_id == MQTT_EVENT_CONNECTED) {
        s_mqtt_connected = true;
        ESP_LOGI(TAG, "MQTT connected");
        mqtt_publisher_publish_availability(true);
        mqtt_publisher_publish_discovery();
        if (s_mqtt_connected_cb != nullptr) {
            s_mqtt_connected_cb();
        }
    } else if (event_id == MQTT_EVENT_DISCONNECTED) {
        s_mqtt_connected = false;
        ESP_LOGW(TAG, "MQTT disconnected");
    } else if (event_id == MQTT_EVENT_ERROR) {
        auto *event = static_cast<esp_mqtt_event_handle_t>(event_data);
        if (event->error_handle->error_type == MQTT_ERROR_TYPE_CONNECTION_REFUSED) {
            ESP_LOGE(TAG, "MQTT refused, code=%d (1=bad protocol, 4=bad user/pass, 5=not authorized)",
                     event->error_handle->connect_return_code);
        } else if (event->error_handle->error_type == MQTT_ERROR_TYPE_TCP_TRANSPORT) {
            ESP_LOGE(TAG, "MQTT TCP error, errno=%d", event->error_handle->esp_transport_sock_errno);
        } else {
            ESP_LOGE(TAG, "MQTT error type=%d", event->error_handle->error_type);
        }
    }
}

static bool publish_raw(const char *topic, const char *payload, bool retain) {
    if (s_client == nullptr || topic == nullptr || payload == nullptr) {
        return false;
    }
    int msg_id = esp_mqtt_client_publish(s_client, topic, payload, 0, retain ? 1 : 0, retain ? 1 : 0);
    return msg_id >= 0;
}

static void publish_discovery_for_char(size_t index, const char_config_t *ch) {
    if (!s_cfg.mqtt.discovery || s_client == nullptr) {
        return;
    }

    cJSON *root = cJSON_CreateObject();
    if (root == nullptr) {
        return;
    }

    const char *component = ch->type == CHAR_TYPE_TEMPERATURE ? "sensor" : "binary_sensor";
    std::string object_id = std::string("wein_") + s_cfg.device_id + "_" + std::to_string(index);

    cJSON_AddStringToObject(root, "name", ch->topic);
    cJSON_AddStringToObject(root, "state_topic", ch->topic);
    cJSON_AddStringToObject(root, "unique_id", object_id.c_str());
    cJSON *avail = make_sensor_availability();
    if (avail != nullptr) {
        cJSON_AddItemToObject(root, "availability", avail);
        cJSON_AddStringToObject(root, "availability_mode", "all");
    }

    if (ch->type == CHAR_TYPE_TEMPERATURE) {
        cJSON_AddStringToObject(root, "unit_of_measurement", "°C");
        cJSON_AddStringToObject(root, "device_class", "temperature");
        cJSON_AddStringToObject(root, "state_class", "measurement");
    } else {
        cJSON_AddStringToObject(root, "device_class", "opening");
        cJSON_AddStringToObject(root, "payload_on", "true");
        cJSON_AddStringToObject(root, "payload_off", "false");
    }

    cJSON *device = make_device_object();
    if (device != nullptr) {
        cJSON_AddItemToObject(root, "device", device);
    }

    publish_discovery_json(component, object_id.c_str(), root);
}

static void publish_discovery_connectivity(void) {
    if (!s_cfg.mqtt.discovery || s_client == nullptr) {
        return;
    }

    {
        cJSON *root = cJSON_CreateObject();
        std::string object_id = std::string("wein_") + s_cfg.device_id + "_gateway";
        if (root != nullptr) {
            cJSON_AddStringToObject(root, "name", "Wein Gateway");
            cJSON_AddStringToObject(root, "state_topic", s_status_topic);
            cJSON_AddStringToObject(root, "unique_id", object_id.c_str());
            cJSON_AddStringToObject(root, "device_class", "connectivity");
            cJSON_AddStringToObject(root, "payload_on", "online");
            cJSON_AddStringToObject(root, "payload_off", "offline");
            cJSON *device = make_device_object();
            if (device != nullptr) {
                cJSON_AddItemToObject(root, "device", device);
            }
            publish_discovery_json("binary_sensor", object_id.c_str(), root);
        }
    }

    {
        cJSON *root = cJSON_CreateObject();
        std::string object_id = std::string("wein_") + s_cfg.device_id + "_ble";
        if (root != nullptr) {
            cJSON_AddStringToObject(root, "name", "Wein Sensor BLE");
            cJSON_AddStringToObject(root, "state_topic", s_ble_connected_topic);
            cJSON_AddStringToObject(root, "unique_id", object_id.c_str());
            cJSON_AddStringToObject(root, "device_class", "connectivity");
            cJSON_AddStringToObject(root, "payload_on", "true");
            cJSON_AddStringToObject(root, "payload_off", "false");
            cJSON *avail = make_gateway_availability();
            if (avail != nullptr) {
                cJSON_AddItemToObject(root, "availability", avail);
            }
            cJSON *device = make_device_object();
            if (device != nullptr) {
                cJSON_AddItemToObject(root, "device", device);
            }
            publish_discovery_json("binary_sensor", object_id.c_str(), root);
        }
    }
}

bool mqtt_publisher_init(const app_config_t *cfg) {
    if (cfg == nullptr) {
        return false;
    }
    s_cfg = *cfg;

    esp_err_t err = nvs_flash_init();
    if (err == ESP_ERR_NVS_NO_FREE_PAGES || err == ESP_ERR_NVS_NEW_VERSION_FOUND) {
        ESP_ERROR_CHECK(nvs_flash_erase());
        err = nvs_flash_init();
    }
    ESP_ERROR_CHECK(err);

    ESP_ERROR_CHECK(esp_netif_init());
    ESP_ERROR_CHECK(esp_event_loop_create_default());
    esp_netif_t *sta_netif = esp_netif_create_default_wifi_sta();
    if (sta_netif != nullptr) {
        ESP_ERROR_CHECK(esp_netif_set_hostname(sta_netif, WIFI_HOSTNAME));
    }

    wifi_init_config_t wifi_init = WIFI_INIT_CONFIG_DEFAULT();
    ESP_ERROR_CHECK(esp_wifi_init(&wifi_init));
    ESP_ERROR_CHECK(esp_event_handler_register(WIFI_EVENT, ESP_EVENT_ANY_ID, &wifi_event_handler, nullptr));
    ESP_ERROR_CHECK(esp_event_handler_register(IP_EVENT, IP_EVENT_STA_GOT_IP, &wifi_event_handler, nullptr));

    wifi_config_t wifi_cfg = {};
    copy_field(reinterpret_cast<char *>(wifi_cfg.sta.ssid), sizeof(wifi_cfg.sta.ssid), s_cfg.wifi.ssid);
    copy_field(reinterpret_cast<char *>(wifi_cfg.sta.password), sizeof(wifi_cfg.sta.password), s_cfg.wifi.password);
    wifi_cfg.sta.threshold.authmode = WIFI_AUTH_WPA2_PSK;
    ESP_ERROR_CHECK(esp_wifi_set_mode(WIFI_MODE_STA));
    ESP_ERROR_CHECK(esp_wifi_set_config(WIFI_IF_STA, &wifi_cfg));
    ESP_ERROR_CHECK(esp_wifi_start());

    std::snprintf(s_mqtt_uri, sizeof(s_mqtt_uri), "mqtt://%s:%u", s_cfg.mqtt.host, s_cfg.mqtt.port);
    std::snprintf(s_status_topic, sizeof(s_status_topic), "wein/%s/status", s_cfg.device_id);
    std::snprintf(s_ble_connected_topic, sizeof(s_ble_connected_topic), "wein/%s/ble_connected", s_cfg.device_id);

    esp_mqtt_client_config_t mqtt_cfg = {};
    mqtt_cfg.broker.address.uri = s_mqtt_uri;
    if (std::strcmp(s_cfg.mqtt.protocol, "5") == 0) {
#ifdef CONFIG_MQTT_PROTOCOL_5
        mqtt_cfg.session.protocol_ver = MQTT_PROTOCOL_V_5;
        ESP_LOGI(TAG, "MQTT broker %s:%u protocol 5.0", s_cfg.mqtt.host, s_cfg.mqtt.port);
#else
        ESP_LOGE(TAG, "config requests MQTT 5 but firmware lacks CONFIG_MQTT_PROTOCOL_5 — rebuild with sdkconfig.defaults");
        mqtt_cfg.session.protocol_ver = MQTT_PROTOCOL_V_3_1_1;
#endif
    } else {
        mqtt_cfg.session.protocol_ver = MQTT_PROTOCOL_V_3_1_1;
        ESP_LOGI(TAG, "MQTT broker %s:%u protocol 3.1.1", s_cfg.mqtt.host, s_cfg.mqtt.port);
    }
    mqtt_cfg.credentials.client_id = s_cfg.device_id;
    if (s_cfg.mqtt.username[0] != '\0') {
        mqtt_cfg.credentials.username = s_cfg.mqtt.username;
        mqtt_cfg.credentials.authentication.password = s_cfg.mqtt.password;
    }
    mqtt_cfg.session.keepalive = 60;
    mqtt_cfg.session.last_will.topic = s_status_topic;
    mqtt_cfg.session.last_will.msg = "offline";
    mqtt_cfg.session.last_will.qos = 1;
    mqtt_cfg.session.last_will.retain = 1;

    s_client = esp_mqtt_client_init(&mqtt_cfg);
    return s_client != nullptr;
}

bool mqtt_publisher_start(void) {
    if (s_client == nullptr) {
        return false;
    }
    esp_mqtt_client_register_event(s_client, MQTT_EVENT_ANY, mqtt_event_handler, nullptr);
    return esp_mqtt_client_start(s_client) == ESP_OK;
}

bool mqtt_publisher_is_connected(void) {
    return s_mqtt_connected;
}

bool mqtt_publisher_publish(const char *topic, const char *payload, bool retain) {
    return publish_raw(topic, payload, retain);
}

bool mqtt_publisher_publish_availability(bool online) {
    return publish_raw(s_status_topic, online ? "online" : "offline", true);
}

bool mqtt_publisher_publish_ble_connected(bool connected) {
    bool ok = publish_raw(s_ble_connected_topic, connected ? "true" : "false", true);
    if (!connected) {
        mqtt_publisher_clear_sensor_states();
    }
    return ok;
}

void mqtt_publisher_clear_sensor_states(void) {
    for (size_t i = 0; i < s_cfg.sensor.characteristic_count; ++i) {
        publish_raw(s_cfg.sensor.characteristics[i].topic, "", true);
    }
}

void mqtt_publisher_set_connected_cb(mqtt_connected_cb_t cb) {
    s_mqtt_connected_cb = cb;
}

void mqtt_publisher_publish_discovery(void) {
    publish_discovery_connectivity();
    for (size_t i = 0; i < s_cfg.sensor.characteristic_count; ++i) {
        publish_discovery_for_char(i, &s_cfg.sensor.characteristics[i]);
    }
}
