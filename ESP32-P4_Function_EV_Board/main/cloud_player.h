#pragma once

#include <stdint.h>

#include "esp_err.h"

typedef enum {
    CLOUD_PLAYER_STATE_IDLE,
    CLOUD_PLAYER_STATE_WAITING_WIFI,
    CLOUD_PLAYER_STATE_SYNCING_TIME,
    CLOUD_PLAYER_STATE_FETCHING_MANIFEST,
    CLOUD_PLAYER_STATE_WAITING_NEW_GENERATION,
    CLOUD_PLAYER_STATE_DOWNLOADING,
    CLOUD_PLAYER_STATE_READY,
    CLOUD_PLAYER_STATE_ERROR,
    CLOUD_PLAYER_STATE_COUNT,
} cloud_player_state_t;

typedef struct {
    cloud_player_state_t state;
    uint32_t downloaded_frames;
    uint32_t total_frames;
    esp_err_t last_error;
} cloud_player_status_t;

esp_err_t cloud_player_start(void);
esp_err_t cloud_player_wait_ready(uint32_t timeout_ms);
void cloud_player_get_status(cloud_player_status_t *status);
esp_err_t cloud_player_play_until_return(void);
void cloud_player_stop(void);
void cloud_player_request_poll(void);
