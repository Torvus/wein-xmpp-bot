#pragma once

#include "app_config.h"

#include <stdbool.h>

typedef void (*mqtt_connected_cb_t)(void);

bool mqtt_publisher_init(const app_config_t *cfg);
bool mqtt_publisher_start(void);
bool mqtt_publisher_is_connected(void);
bool mqtt_publisher_publish(const char *topic, const char *payload, bool retain);
bool mqtt_publisher_publish_availability(bool online);
bool mqtt_publisher_publish_ble_connected(bool connected);
void mqtt_publisher_clear_sensor_states(void);
void mqtt_publisher_set_connected_cb(mqtt_connected_cb_t cb);
void mqtt_publisher_publish_discovery(void);
