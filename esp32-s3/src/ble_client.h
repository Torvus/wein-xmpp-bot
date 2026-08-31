#pragma once

#include "app_config.h"

#include <stdbool.h>

typedef void (*ble_notify_cb_t)(const char_config_t *ch, const char *payload);

bool ble_client_init(const app_config_t *cfg, ble_notify_cb_t on_notify);
void ble_client_start(void);
void ble_client_set_connected_cb(void (*cb)(bool connected));
bool ble_client_is_connected(void);
