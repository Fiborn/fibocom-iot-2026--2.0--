#pragma once

#include "esp_err.h"

esp_err_t animation_player_init(void);
esp_err_t animation_player_play(const char *path);
