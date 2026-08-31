idf_component_register(
    SRCS
        "main.cpp"
        "app_config.cpp"
        "payload_parser.cpp"
        "ble_client.cpp"
        "mqtt_publisher.cpp"
    INCLUDE_DIRS "."
    REQUIRES
        nvs_flash
        esp_wifi
        esp_event
        esp_netif
        mqtt
        json
        spiffs
        bt
)
