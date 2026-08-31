#pragma once

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

bool parse_temperature(const uint8_t *data, size_t len, float *out);
bool parse_boolean(const uint8_t *data, size_t len, bool *out);
