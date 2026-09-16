#pragma once

#include <stdbool.h>

#include "esp_err.h"

esp_err_t sc171_udp_init(void);
esp_err_t sc171_udp_send(char signal);
bool sc171_udp_is_connected(void);
