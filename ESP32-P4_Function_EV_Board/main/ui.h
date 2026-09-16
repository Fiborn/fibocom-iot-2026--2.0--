#pragma once

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

#include "esp_err.h"
#include "wifi_manager.h"

#define P4CONTROL_FRAME_WIDTH  640
#define P4CONTROL_FRAME_HEIGHT 480

typedef enum {
    UI_ACTION_ROUND_START,
    UI_ACTION_ROUND_END,
    UI_ACTION_CHALLENGE_A_TO_B,
    UI_ACTION_CHALLENGE_B_TO_A,
} ui_action_t;

typedef void (*ui_action_handler_t)(ui_action_t action);

esp_err_t ui_init(ui_action_handler_t action_handler);
void ui_show_scoreboard(void);
void ui_show_media(const char *status, bool show_return_button);
esp_err_t ui_show_challenge_image(void);
void ui_set_media_status(const char *status);
void ui_set_return_visible(bool visible);
bool ui_take_return_requested(void);
void ui_set_return_requested(void);
void ui_set_wifi_status(bool connected);
void ui_set_sc171_status(bool connected);
void ui_set_sc171_progress(int percent);
void ui_set_pc_progress(int percent);
void ui_hide_progress_bars(void);
void ui_hide_media_image(void);
esp_err_t ui_decode_and_show_jpeg(
    const uint8_t *jpeg_data,
    size_t jpeg_size,
    uint32_t index,
    uint32_t count
);
