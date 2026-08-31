# Wein BLE → MQTT (ESP32-S3)

Прошивка для ESP32-S3: подключается к BLE-датчику **START-STOP**, подписывается на GATT notify по заданным UUID и публикует значения в MQTT. Без порогов, без XMPP — только ретрансляция notify.

## Что делает

1. WiFi → MQTT-брокер
2. BLE central → датчик START-STOP (MAC из конфига)
3. Notify по UUID → парсинг (температура / boolean) → MQTT-топик

## Железо

- Плата **ESP32-S3-DevKitC-1** (или совместимая с BLE)
- BLE-датчик Wein START-STOP в зоне доступа

## ПО

Нужен **PlatformIO Core 6.x**. Пакет из репозитория дистрибутива (`apt install platformio`, версия 4.x) **не работает** с Python 3.12 — будет ошибка `resultcallback`.

### Установка PlatformIO (рекомендуется: venv в проекте)

```bash
cd esp32-s3
python3 -m venv .venv
.venv/bin/pip install -U platformio
```

Дальше используйте `.venv/bin/pio` вместо `pio`, либо активируйте окружение:

```bash
source .venv/bin/activate
pio --version   # должно быть 6.x
```

Альтернатива — [pipx](https://pipx.pypa.io/): `pipx install platformio`.

## Быстрый старт

```bash
cd esp32-s3
cp data/config.example.json data/config.json
# Отредактируйте data/config.json: WiFi, MQTT, MAC датчика, UUID

# если PlatformIO установлен в .venv (см. выше):
.venv/bin/pio run -t uploadfs    # загрузить config.json в SPIFFS
.venv/bin/pio run -t upload      # прошить firmware
.venv/bin/pio device monitor     # serial log
```

## Конфигурация

Файл `data/config.json` (загружается в раздел SPIFFS):

| Секция | Поля |
|--------|------|
| `wifi` | `ssid`, `password` |

Имя клиента в списке роутера: **Moonshine-Controller** (DHCP hostname).
| `mqtt` | `host`, `port`, `username`, `password`, `discovery`, `discovery_prefix` |
| `sensor` | `mac_address`, `ble_passkey`, `reconnect_*`, `characteristics[]` |

Каждая характеристика:

```json
{
  "uuid": "a8e8e254-ce9d-42e7-9170-aa67f1694a77",
  "topic": "wein/b0cbd8ee41f6/temperature",
  "type": "temperature"
}
```

Типы: `temperature` (float °C) или `boolean` (`true`/`false`).

## BLE-сопряжение

Датчик START-STOP показывает код на дисплее **кратко** при pairing. Раньше прошивка сразу отправляла `000000` и код исчезал.

**Первое сопряжение (вариант A — через serial monitor):**

1. Оставьте `"ble_passkey": 0` в config.json
2. Запустите `.venv/bin/pio device monitor`
3. При появлении `>>> Введите код с дисплея датчика` быстро введите 6 цифр с дисплея START-STOP и Enter
4. После успешного pairing bond сохранится в NVS

**Вариант B — заранее в config.json:**

1. Увидьте код на дисплее (можно через Python `ble_sensor.py` + pairing agent на ПК)
2. Запишите `"ble_passkey": 123456` в config.json → `uploadfs` → RESET

**Если в логе `Passkey action=4` (NUMCMP):** подтвердите код **кнопками на датчике** (прошивка принимает автоматически).

**Если `Passkey action=3` (DISP):** введите показанный в логе код **на датчике** кнопками.

При сбросе bond: `.venv/bin/pio run -t erase` и повторное сопряжение.

## MQTT-топики

| Топик | Значение |
|-------|----------|
| `wein/<device_id>/temperature` | `23.45` |
| `wein/<device_id>/valve_control` | `true` / `false` |
| `wein/<device_id>/valve_state` | `true` / `false` |
| `wein/<device_id>/status` | `online` / `offline` (LWT при обрыве связи ESP) |
| `wein/<device_id>/ble_connected` | `true` / `false` |

`<device_id>` — MAC без двоеточий, нижний регистр (например `b0cbd8ee41f6`).

При `mqtt.discovery: true` прошивка публикует конфиги Home Assistant MQTT discovery.

**Доступность в HA:** сущности температуры и клапанов помечаются недоступными (`unavailable`), если ESP offline **или** датчик не подключён по BLE. Дополнительно создаются binary_sensor **Wein Gateway** и **Wein Sensor BLE** (`device_class: connectivity`).

При отключении BLE retained-значения сенсоров сбрасываются (пустой payload).

## Ожидание датчика (нагрузка и переподключение)

Когда датчик недоступен, ESP32 **не** держит BLE-радио постоянно включённым:

1. Пауза `reconnect_interval_sec` (по умолчанию **600 с**) — CPU и BLE в покое
2. Короткое **passive**-сканирование `reconnect_scan_timeout_sec` (**15 с**)
3. Подключение **только если MAC замечен в эфире** (без 20-секундного «вслепую» connect)
4. При успешной сессии — notify + опрос `valve_control` раз в 2 с

Первое сканирование после загрузки — **active** (быстрее найти датчик). Дальше — passive (меньше нагрев).

Максимальная задержка обнаружения включённого датчика: ≈ `reconnect_interval_sec + reconnect_scan_timeout_sec` (615 с при настройках по умолчанию). Для более быстрой реакции уменьшите `reconnect_interval_sec`, например до `60`.

Wi‑Fi и MQTT остаются активными — это нормально для постоянного реле в HA.

## Home Assistant

1. Установите add-on **Mosquitto broker**
2. Настройте MQTT integration
3. При включённом discovery сущности появятся автоматически
4. Пороги и оповещения — через automations на MQTT-топиках, например:

```yaml
automation:
  - alias: "Wein hot"
    trigger:
      - platform: mqtt
        topic: "wein/b0cbd8ee41f6/temperature"
    condition:
      - condition: template
        value_template: "{{ trigger.payload | float > 30 }}"
    action:
      - service: notify.mobile_app
        data:
          message: "Температура {{ trigger.payload }} °C"
```

## Отладка

```bash
# Проверить MQTT
mosquitto_sub -h 192.168.1.10 -t 'wein/#' -v

# Serial log
pio device monitor -b 115200
```

Типичные проблемы:

| Симптом | Решение |
|---------|---------|
| `Cannot open /config/config.json` | `cp data/config.example.json data/config.json && .venv/bin/pio run -t uploadfs` |
| `Connection refused, bad protocol` | Брокер отклонил версию MQTT (код 1). См. раздел ниже |
| BLE не подключается | Проверьте MAC, passkey, расстояние до датчика |
| MQTT offline | Проверьте host/port/credentials |

### MQTT: `Connection refused, bad protocol`

WiFi при этом уже работает. Брокер **ответил**, но вернул CONNACK с кодом **1** — «неподходящая версия протокола MQTT» (это не TLS и не пароль).

**1. Проверьте брокер с компьютера:**

```bash
mosquitto_sub -h 192.168.1.2 -p 1883 -u wein -P wein -t test -V mqttv311
mosquitto_sub -h 192.168.1.2 -p 1883 -u wein -P wein -t test -V mqttv5
```

**2. Home Assistant → Mosquitto add-on → Configuration:**

```yaml
logins:
  - username: wein
    password: wein
```

**3. Add-on → Network** — порт **1883** проброшен наружу.

**4. В `config.json`** укажите протокол, который сработал в `mosquitto_sub`:

```json
"protocol": "3.1.1"
```

или `"protocol": "5"`, затем `.venv/bin/pio run -t uploadfs` и RESET.

**5.** Убедитесь, что `192.168.1.2:1883` — Mosquitto, а не веб HA (8123).

## Связь с основным репозиторием

- Python-приложение (`main.py`, XMPP) **не используется** при работе через ESP32
- Интеграция `home-assistant/custom_components/wein/` — альтернативный путь (прямой BLE из HA); с ESP32-шлюзом данные идут через MQTT
