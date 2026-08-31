#pragma once

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

#define APP_MAX_CHARS 8
#define APP_UUID_LEN 37
#define APP_TOPIC_LEN 128
#define APP_DEVICE_ID_LEN 13

typedef enum {
    CHAR_TYPE_TEMPERATURE,
    CHAR_TYPE_BOOLEAN,
} char_type_t;

typedef struct {
    char uuid[APP_UUID_LEN];
    char topic[APP_TOPIC_LEN];
    char_type_t type;
    uint16_t val_handle;
    uint16_t cccd_handle;
    uint8_t properties;
    bool subscribed;
} char_config_t;

typedef struct {
    char ssid[32];
    char password[64];
} app_wifi_config_t;

typedef struct {
    char host[64];
    uint16_t port;
    char username[32];
    char password[64];
    bool discovery;
    char discovery_prefix[32];
    char protocol[8];  // "3.1.1" (default) or "5"
} mqtt_config_t;

typedef struct {
    char mac_address[18];
    uint32_t ble_passkey;
    uint32_t reconnect_interval_sec;
    uint32_t reconnect_scan_timeout_sec;
    char_config_t characteristics[APP_MAX_CHARS];
    size_t characteristic_count;
} sensor_config_t;

typedef struct {
    app_wifi_config_t wifi;
    mqtt_config_t mqtt;
    sensor_config_t sensor;
    char device_id[APP_DEVICE_ID_LEN];
} app_config_t;

bool app_config_load(app_config_t *cfg);
const char *char_type_to_string(char_type_t type);
