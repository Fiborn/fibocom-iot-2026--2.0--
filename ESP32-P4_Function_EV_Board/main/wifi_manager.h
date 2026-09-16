#pragma once

#include <stdbool.h>
#include <stdint.h>

#include "esp_err.h"

esp_err_t wifi_manager_init(void);
esp_err_t wifi_manager_wait_connected(uint32_t timeout_ms);
esp_err_t wifi_manager_reconnect(void);
bool wifi_manager_is_connected(void);
