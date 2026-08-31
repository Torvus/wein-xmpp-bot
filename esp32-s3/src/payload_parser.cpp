#include "payload_parser.h"

#include <cctype>
#include <cmath>
#include <cstdlib>
#include <cstring>

bool parse_temperature(const uint8_t *data, size_t len, float *out) {
    if (data == nullptr || out == nullptr || len == 0) {
        return false;
    }
    if (len == 2) {
        int16_t raw = static_cast<int16_t>(data[0] | (static_cast<uint16_t>(data[1]) << 8));
        *out = static_cast<float>(raw) / 100.0f;
        return true;
    }
    if (len == 4) {
        uint32_t bits = static_cast<uint32_t>(data[0]) |
                        (static_cast<uint32_t>(data[1]) << 8) |
                        (static_cast<uint32_t>(data[2]) << 16) |
                        (static_cast<uint32_t>(data[3]) << 24);
        float value;
        std::memcpy(&value, &bits, sizeof(value));
        *out = value;
        return true;
    }
    char buf[32];
    size_t copy_len = len < sizeof(buf) - 1 ? len : sizeof(buf) - 1;
    std::memcpy(buf, data, copy_len);
    buf[copy_len] = '\0';
    for (size_t i = 0; buf[i] != '\0'; ++i) {
        if (buf[i] == ',') {
            buf[i] = '.';
        }
    }
    char *end = nullptr;
    float value = std::strtof(buf, &end);
    if (end == buf) {
        return false;
    }
    *out = value;
    return true;
}

bool parse_boolean(const uint8_t *data, size_t len, bool *out) {
    if (data == nullptr || out == nullptr || len == 0) {
        return false;
    }
    char buf[16];
    size_t copy_len = len < sizeof(buf) - 1 ? len : sizeof(buf) - 1;
    std::memcpy(buf, data, copy_len);
    buf[copy_len] = '\0';
    for (size_t i = 0; buf[i] != '\0'; ++i) {
        buf[i] = static_cast<char>(std::tolower(static_cast<unsigned char>(buf[i])));
    }
    if (std::strcmp(buf, "true") == 0) {
        *out = true;
        return true;
    }
    if (std::strcmp(buf, "false") == 0) {
        *out = false;
        return true;
    }
    return false;
}
