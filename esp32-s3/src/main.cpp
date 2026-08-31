#include "app_config.h"
#include "ble_client.h"
#include "mqtt_publisher.h"

#include "esp_log.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"

static const char *TAG = "main";
static app_config_t s_cfg;

static void on_ble_connected(bool connected) {
    mqtt_publisher_publish_ble_connected(connected);
}

static void on_mqtt_connected(void) {
    mqtt_publisher_publish_ble_connected(ble_client_is_connected());
}

static void on_ble_notify(const char_config_t *ch, const char *payload) {
    if (ch == nullptr || payload == nullptr) {
        return;
    }
    ESP_LOGI(TAG, "Notify %s -> %s = %s", ch->uuid, ch->topic, payload);
    mqtt_publisher_publish(ch->topic, payload, true);
}

extern "C" void app_main(void) {
    ESP_LOGI(TAG, "Wein BLE -> MQTT relay");

    if (!app_config_load(&s_cfg)) {
        ESP_LOGE(TAG, "Config load failed");
        return;
    }

    if (!mqtt_publisher_init(&s_cfg)) {
        ESP_LOGE(TAG, "MQTT init failed");
        return;
    }
    if (!mqtt_publisher_start()) {
        ESP_LOGE(TAG, "MQTT start failed");
        return;
    }
    mqtt_publisher_set_connected_cb(on_mqtt_connected);

    ble_client_set_connected_cb(on_ble_connected);
    if (!ble_client_init(&s_cfg, on_ble_notify)) {
        ESP_LOGE(TAG, "BLE init failed");
        return;
    }
    ble_client_start();

    while (true) {
        vTaskDelay(pdMS_TO_TICKS(1000));
    }
}
