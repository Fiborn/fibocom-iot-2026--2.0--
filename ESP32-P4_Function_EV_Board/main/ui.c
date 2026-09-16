#include "ui.h"

#include <inttypes.h>
#include <limits.h>
#include <stdio.h>
#include <string.h>

#include "bsp/esp-bsp.h"
#include "esp_attr.h"
#include "esp_check.h"
#include "esp_heap_caps.h"
#include "esp_jpeg_dec.h"
#include "esp_log.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "lvgl.h"

#define SCREEN_WIDTH  1024
#define SCREEN_HEIGHT 600
#define RGB565_FRAME_BYTES ((size_t)P4CONTROL_FRAME_WIDTH * P4CONTROL_FRAME_HEIGHT * 2)

extern const uint8_t scoreboard_rgb565_start[] asm("_binary_scoreboard_rgb565_start");
extern const uint8_t scoreboard_rgb565_end[] asm("_binary_scoreboard_rgb565_end");
extern const uint8_t challenge_jpg_start[] asm("_binary_challenge_jpg_start");
extern const uint8_t challenge_jpg_end[] asm("_binary_challenge_jpg_end");

static const char *TAG = "ui";
static lv_display_t *s_display;
static lv_obj_t *s_score_root;
static lv_obj_t *s_media_root;
static lv_obj_t *s_media_image;
static lv_obj_t *s_media_status;
static lv_obj_t *s_return_button;
static lv_obj_t *s_sc171_progress_bar;
static lv_obj_t *s_sc171_progress_label;
static lv_obj_t *s_pc_progress_bar;
static lv_obj_t *s_pc_progress_label;
static lv_obj_t *s_score_a_label;
static lv_obj_t *s_score_b_label;
static lv_obj_t *s_challenges_a_label;
static lv_obj_t *s_challenges_b_label;
static lv_obj_t *s_total_label;
static lv_obj_t *s_wifi_label;
static lv_obj_t *s_sc171_label;
static lv_image_dsc_t s_scoreboard_image;
static lv_image_dsc_t s_frame_images[2];
static uint8_t *s_frame_pixels[2];
static unsigned s_next_frame;
static ui_action_handler_t s_action_handler;
static volatile bool s_return_requested;

static unsigned s_score_a;
static unsigned s_score_b;
static unsigned s_challenges_a = 2;
static unsigned s_challenges_b = 2;
static unsigned s_total_a;
static unsigned s_total_b;

#define RETAINED_STATE_MAGIC 0x50345331U

typedef struct {
    uint32_t magic;
    unsigned score_a;
    unsigned score_b;
    unsigned challenges_a;
    unsigned challenges_b;
    unsigned total_a;
    unsigned total_b;
} retained_score_state_t;

static RTC_NOINIT_ATTR retained_score_state_t s_retained_state;

typedef enum {
    CONTROL_SCORE_A_PLUS,
    CONTROL_SCORE_B_PLUS,
    CONTROL_CHALLENGE_A_MINUS,
    CONTROL_CHALLENGE_B_MINUS,
    CONTROL_ROUND_START,
    CONTROL_ROUND_END,
    CONTROL_CHALLENGE_A_TO_B,
    CONTROL_CHALLENGE_B_TO_A,
    CONTROL_A_WINS,
    CONTROL_B_WINS,
    CONTROL_GAME_END,
    CONTROL_RESET,
} control_id_t;

static void update_values(void)
{
    lv_label_set_text_fmt(s_score_a_label, "%u", s_score_a);
    lv_label_set_text_fmt(s_score_b_label, "%u", s_score_b);
    lv_label_set_text_fmt(s_challenges_a_label, "%u", s_challenges_a);
    lv_label_set_text_fmt(s_challenges_b_label, "%u", s_challenges_b);
    lv_label_set_text_fmt(s_total_label, "%u : %u", s_total_a, s_total_b);
    s_retained_state = (retained_score_state_t) {
        .magic = RETAINED_STATE_MAGIC,
        .score_a = s_score_a,
        .score_b = s_score_b,
        .challenges_a = s_challenges_a,
        .challenges_b = s_challenges_b,
        .total_a = s_total_a,
        .total_b = s_total_b,
    };
}

static void restore_retained_values(void)
{
    if (s_retained_state.magic != RETAINED_STATE_MAGIC) {
        return;
    }
    s_score_a = s_retained_state.score_a;
    s_score_b = s_retained_state.score_b;
    s_challenges_a = s_retained_state.challenges_a;
    s_challenges_b = s_retained_state.challenges_b;
    s_total_a = s_retained_state.total_a;
    s_total_b = s_retained_state.total_b;
    ESP_LOGW(TAG, "restored retained scoreboard values");
}

static void reset_round(void)
{
    s_score_a = 0;
    s_score_b = 0;
    s_challenges_a = 2;
    s_challenges_b = 2;
    update_values();
}

static void control_event(lv_event_t *event)
{
    if (lv_event_get_code(event) != LV_EVENT_CLICKED) {
        return;
    }
    control_id_t id = (control_id_t)(uintptr_t)lv_event_get_user_data(event);
    switch (id) {
    case CONTROL_SCORE_A_PLUS:
        ++s_score_a;
        update_values();
        break;
    case CONTROL_SCORE_B_PLUS:
        ++s_score_b;
        update_values();
        break;
    case CONTROL_CHALLENGE_A_MINUS:
        if (s_challenges_a > 0) {
            --s_challenges_a;
            update_values();
        }
        break;
    case CONTROL_CHALLENGE_B_MINUS:
        if (s_challenges_b > 0) {
            --s_challenges_b;
            update_values();
        }
        break;
    case CONTROL_A_WINS:
        ++s_total_a;
        update_values();
        break;
    case CONTROL_B_WINS:
        ++s_total_b;
        update_values();
        break;
    case CONTROL_GAME_END:
        reset_round();
        break;
    case CONTROL_RESET:
        s_total_a = 0;
        s_total_b = 0;
        reset_round();
        break;
    case CONTROL_ROUND_START:
        if (s_action_handler != NULL) {
            s_action_handler(UI_ACTION_ROUND_START);
        }
        break;
    case CONTROL_ROUND_END:
        if (s_action_handler != NULL) {
            s_action_handler(UI_ACTION_ROUND_END);
        }
        break;
    case CONTROL_CHALLENGE_A_TO_B:
        if (s_action_handler != NULL) {
            s_action_handler(UI_ACTION_CHALLENGE_A_TO_B);
        }
        break;
    case CONTROL_CHALLENGE_B_TO_A:
        if (s_action_handler != NULL) {
            s_action_handler(UI_ACTION_CHALLENGE_B_TO_A);
        }
        break;
    }
}

static lv_obj_t *create_hotspot(int x, int y, int width, int height, control_id_t id)
{
    lv_obj_t *hotspot = lv_obj_create(s_score_root);
    lv_obj_set_pos(hotspot, x, y);
    lv_obj_set_size(hotspot, width, height);
    lv_obj_remove_flag(hotspot, LV_OBJ_FLAG_SCROLLABLE);
    lv_obj_set_style_radius(hotspot, 6, LV_PART_MAIN);
    lv_obj_set_style_border_width(hotspot, 0, LV_PART_MAIN);
    lv_obj_set_style_bg_opa(hotspot, LV_OPA_TRANSP, LV_PART_MAIN);
    lv_obj_set_style_bg_color(hotspot, lv_palette_main(LV_PALETTE_BLUE), LV_PART_MAIN | LV_STATE_PRESSED);
    lv_obj_set_style_bg_opa(hotspot, LV_OPA_20, LV_PART_MAIN | LV_STATE_PRESSED);
    lv_obj_add_event_cb(hotspot, control_event, LV_EVENT_CLICKED, (void *)(uintptr_t)id);
    return hotspot;
}

static lv_obj_t *create_value(int x, int y, int width, int height, const lv_font_t *font)
{
    lv_obj_t *area = lv_obj_create(s_score_root);
    lv_obj_set_pos(area, x, y);
    lv_obj_set_size(area, width, height);
    lv_obj_remove_flag(area, LV_OBJ_FLAG_SCROLLABLE);
    lv_obj_set_style_radius(area, 0, LV_PART_MAIN);
    lv_obj_set_style_border_width(area, 0, LV_PART_MAIN);
    lv_obj_set_style_pad_all(area, 0, LV_PART_MAIN);
    lv_obj_set_style_bg_color(area, lv_color_white(), LV_PART_MAIN);
    lv_obj_set_style_bg_opa(area, LV_OPA_COVER, LV_PART_MAIN);

    lv_obj_t *label = lv_label_create(area);
    lv_obj_set_style_text_font(label, font, LV_PART_MAIN);
    lv_obj_set_style_text_color(label, lv_color_black(), LV_PART_MAIN);
    lv_obj_center(label);
    return label;
}

static void return_event(lv_event_t *event)
{
    if (lv_event_get_code(event) == LV_EVENT_CLICKED) {
        s_return_requested = true;
    }
}

static void create_scoreboard(lv_obj_t *screen)
{
    s_score_root = lv_obj_create(screen);
    lv_obj_set_size(s_score_root, SCREEN_WIDTH, SCREEN_HEIGHT);
    lv_obj_set_pos(s_score_root, 0, 0);
    lv_obj_remove_flag(s_score_root, LV_OBJ_FLAG_SCROLLABLE);
    lv_obj_set_style_border_width(s_score_root, 0, LV_PART_MAIN);
    lv_obj_set_style_pad_all(s_score_root, 0, LV_PART_MAIN);

    size_t image_size = (size_t)(scoreboard_rgb565_end - scoreboard_rgb565_start);
    memset(&s_scoreboard_image, 0, sizeof(s_scoreboard_image));
    s_scoreboard_image.header.magic = LV_IMAGE_HEADER_MAGIC;
    s_scoreboard_image.header.cf = LV_COLOR_FORMAT_RGB565;
    s_scoreboard_image.header.w = SCREEN_WIDTH;
    s_scoreboard_image.header.h = SCREEN_HEIGHT;
    s_scoreboard_image.header.stride = SCREEN_WIDTH * 2;
    s_scoreboard_image.data_size = image_size;
    s_scoreboard_image.data = scoreboard_rgb565_start;

    lv_obj_t *background = lv_image_create(s_score_root);
    lv_image_set_src(background, &s_scoreboard_image);
    lv_obj_set_pos(background, 0, 0);
    lv_obj_remove_flag(background, LV_OBJ_FLAG_CLICKABLE);

    s_challenges_a_label = create_value(315, 78, 28, 28, &lv_font_montserrat_24);
    s_challenges_b_label = create_value(699, 78, 28, 28, &lv_font_montserrat_24);
    s_score_a_label = create_value(269, 258, 70, 72, &lv_font_montserrat_48);
    s_score_b_label = create_value(614, 258, 70, 72, &lv_font_montserrat_48);
    s_total_label = create_value(858, 416, 64, 27, &lv_font_montserrat_20);

    create_hotspot(351, 76, 50, 32, CONTROL_CHALLENGE_A_MINUS);
    create_hotspot(735, 76, 50, 32, CONTROL_CHALLENGE_B_MINUS);
    create_hotspot(90, 447, 45, 48, CONTROL_SCORE_A_PLUS);
    create_hotspot(482, 447, 45, 48, CONTROL_SCORE_B_PLUS);

    create_hotspot(840, 70, 95, 40, CONTROL_ROUND_START);
    create_hotspot(840, 116, 95, 40, CONTROL_ROUND_END);
    create_hotspot(840, 161, 95, 40, CONTROL_CHALLENGE_A_TO_B);
    create_hotspot(840, 205, 95, 40, CONTROL_CHALLENGE_B_TO_A);
    create_hotspot(840, 253, 95, 41, CONTROL_A_WINS);
    create_hotspot(840, 298, 95, 41, CONTROL_B_WINS);
    create_hotspot(840, 343, 95, 42, CONTROL_GAME_END);
    create_hotspot(840, 457, 95, 42, CONTROL_RESET);

    /* WiFi status: top-left */
    s_wifi_label = lv_label_create(s_score_root);
    lv_obj_set_style_text_font(s_wifi_label, &lv_font_montserrat_20, LV_PART_MAIN);
    lv_obj_set_style_text_color(s_wifi_label, lv_color_make(255, 0, 0), LV_PART_MAIN);
    lv_label_set_text(s_wifi_label, "wifi_disconnect");
    lv_obj_set_pos(s_wifi_label, 10, 5);

    /* SC171 status: top-right */
    s_sc171_label = lv_label_create(s_score_root);
    lv_obj_set_style_text_font(s_sc171_label, &lv_font_montserrat_20, LV_PART_MAIN);
    lv_obj_set_style_text_color(s_sc171_label, lv_color_make(255, 0, 0), LV_PART_MAIN);
    lv_label_set_text(s_sc171_label, "SC171_disconnect");
    lv_obj_set_pos(s_sc171_label, 830, 5);

    restore_retained_values();
    update_values();
}

static void create_media(lv_obj_t *screen)
{
    s_media_root = lv_obj_create(screen);
    lv_obj_set_size(s_media_root, SCREEN_WIDTH, SCREEN_HEIGHT);
    lv_obj_set_pos(s_media_root, 0, 0);
    lv_obj_remove_flag(s_media_root, LV_OBJ_FLAG_SCROLLABLE);
    lv_obj_set_style_border_width(s_media_root, 0, LV_PART_MAIN);
    lv_obj_set_style_pad_all(s_media_root, 0, LV_PART_MAIN);
    lv_obj_set_style_bg_color(s_media_root, lv_color_black(), LV_PART_MAIN);
    lv_obj_set_style_bg_opa(s_media_root, LV_OPA_COVER, LV_PART_MAIN);

    s_media_image = lv_image_create(s_media_root);
    lv_obj_center(s_media_image);

    s_media_status = lv_label_create(s_media_root);
    lv_obj_set_style_text_font(s_media_status, &lv_font_montserrat_20, LV_PART_MAIN);
    lv_obj_set_style_text_color(s_media_status, lv_color_white(), LV_PART_MAIN);
    lv_obj_set_style_bg_color(s_media_status, lv_color_black(), LV_PART_MAIN);
    lv_obj_set_style_bg_opa(s_media_status, LV_OPA_70, LV_PART_MAIN);
    lv_obj_set_style_pad_all(s_media_status, 10, LV_PART_MAIN);
    lv_obj_align(s_media_status, LV_ALIGN_TOP_MID, 0, 12);

    /* -- 进度条（等待阶段，全绿色，左下角） -- */

    s_sc171_progress_label = lv_label_create(s_media_root);
    lv_label_set_text(s_sc171_progress_label, "SC171");
    lv_obj_set_style_text_color(s_sc171_progress_label, lv_color_white(), LV_PART_MAIN);
    lv_obj_align(s_sc171_progress_label, LV_ALIGN_BOTTOM_RIGHT, -20, -75);

    s_sc171_progress_bar = lv_bar_create(s_media_root);
    lv_obj_set_size(s_sc171_progress_bar, 320, 14);
    lv_obj_align(s_sc171_progress_bar, LV_ALIGN_BOTTOM_RIGHT, -20, -75);
    lv_bar_set_range(s_sc171_progress_bar, 0, 100);
    lv_bar_set_value(s_sc171_progress_bar, 0, LV_ANIM_OFF);
    lv_obj_set_style_bg_color(s_sc171_progress_bar, lv_color_darken(lv_color_white(), 200), LV_PART_MAIN);
    lv_obj_set_style_bg_opa(s_sc171_progress_bar, LV_OPA_COVER, LV_PART_MAIN);
    lv_obj_set_style_bg_color(s_sc171_progress_bar, lv_color_hex(0x50C050), LV_PART_INDICATOR);
    lv_obj_set_style_bg_opa(s_sc171_progress_bar, LV_OPA_COVER, LV_PART_INDICATOR);
    lv_obj_add_flag(s_sc171_progress_label, LV_OBJ_FLAG_HIDDEN);
    lv_obj_add_flag(s_sc171_progress_bar, LV_OBJ_FLAG_HIDDEN);

    s_pc_progress_label = lv_label_create(s_media_root);
    lv_label_set_text(s_pc_progress_label, "PC");
    lv_obj_set_style_text_color(s_pc_progress_label, lv_color_white(), LV_PART_MAIN);
    lv_obj_align(s_pc_progress_label, LV_ALIGN_BOTTOM_RIGHT, -20, -55);

    s_pc_progress_bar = lv_bar_create(s_media_root);
    lv_obj_set_size(s_pc_progress_bar, 320, 14);
    lv_obj_align(s_pc_progress_bar, LV_ALIGN_BOTTOM_RIGHT, -20, -55);
    lv_bar_set_range(s_pc_progress_bar, 0, 100);
    lv_bar_set_value(s_pc_progress_bar, 0, LV_ANIM_OFF);
    lv_obj_set_style_bg_color(s_pc_progress_bar, lv_color_darken(lv_color_white(), 200), LV_PART_MAIN);
    lv_obj_set_style_bg_opa(s_pc_progress_bar, LV_OPA_COVER, LV_PART_MAIN);
    lv_obj_set_style_bg_color(s_pc_progress_bar, lv_color_hex(0x50C050), LV_PART_INDICATOR);
    lv_obj_set_style_bg_opa(s_pc_progress_bar, LV_OPA_COVER, LV_PART_INDICATOR);
    lv_obj_add_flag(s_pc_progress_label, LV_OBJ_FLAG_HIDDEN);
    lv_obj_add_flag(s_pc_progress_bar, LV_OBJ_FLAG_HIDDEN);

    s_return_button = lv_button_create(s_media_root);
    lv_obj_set_size(s_return_button, 100, 36);
    lv_obj_align(s_return_button, LV_ALIGN_BOTTOM_LEFT, 20, -20);
    lv_obj_add_event_cb(s_return_button, return_event, LV_EVENT_CLICKED, NULL);
    lv_obj_t *label = lv_label_create(s_return_button);
    lv_label_set_text(label, "返回");
    lv_obj_set_style_text_font(label, &lv_font_source_han_sans_sc_16_cjk, LV_PART_MAIN);
    lv_obj_center(label);

    lv_obj_add_flag(s_media_root, LV_OBJ_FLAG_HIDDEN);
}

esp_err_t ui_init(ui_action_handler_t action_handler)
{
    s_action_handler = action_handler;
    for (unsigned slot = 0; slot < 2; ++slot) {
        s_frame_pixels[slot] = heap_caps_aligned_alloc(
            16,
            RGB565_FRAME_BYTES,
            MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT
        );
        if (s_frame_pixels[slot] == NULL) {
            return ESP_ERR_NO_MEM;
        }
        memset(&s_frame_images[slot], 0, sizeof(s_frame_images[slot]));
        s_frame_images[slot].header.magic = LV_IMAGE_HEADER_MAGIC;
        s_frame_images[slot].header.cf = LV_COLOR_FORMAT_RGB565;
        s_frame_images[slot].header.w = P4CONTROL_FRAME_WIDTH;
        s_frame_images[slot].header.h = P4CONTROL_FRAME_HEIGHT;
        s_frame_images[slot].header.stride = P4CONTROL_FRAME_WIDTH * 2;
        s_frame_images[slot].data_size = RGB565_FRAME_BYTES;
        s_frame_images[slot].data = s_frame_pixels[slot];
    }

    s_display = bsp_display_start();
    if (s_display == NULL) {
        return ESP_FAIL;
    }
    bsp_display_lock(0);
    lv_obj_t *screen = lv_display_get_screen_active(s_display);
    lv_obj_set_style_bg_color(screen, lv_color_white(), LV_PART_MAIN);
    lv_obj_set_style_bg_opa(screen, LV_OPA_COVER, LV_PART_MAIN);
    create_scoreboard(screen);
    create_media(screen);
    bsp_display_unlock();
    /*
     * Keep the initial backlight current modest while the ESP32-C6 starts.
     * On the P4 Function EV Board + 1024x600 HMI combination, enabling the
     * backlight at 100% at the same time as the C6 reset/boot can create a
     * large supply transient and make the P4 repeatedly reset.
     */
    ESP_RETURN_ON_ERROR(
        bsp_display_brightness_set(30),
        TAG,
        "backlight brightness setup failed"
    );
    ESP_LOGI(TAG, "UI initialized; startup backlight limited to 30%%");
    return ESP_OK;
}

void ui_set_wifi_status(bool connected)
{
    bsp_display_lock(0);
    if (connected) {
        lv_label_set_text(s_wifi_label, "wifi:#######");
        lv_obj_set_style_text_color(s_wifi_label, lv_color_make(0, 255, 0), LV_PART_MAIN);
    } else {
        lv_label_set_text(s_wifi_label, "wifi_disconnect");
        lv_obj_set_style_text_color(s_wifi_label, lv_color_make(255, 0, 0), LV_PART_MAIN);
    }
    bsp_display_unlock();
}

void ui_set_sc171_status(bool connected)
{
    bsp_display_lock(0);
    if (connected) {
        lv_label_set_text(s_sc171_label, "SC171 ready");
        lv_obj_set_style_text_color(s_sc171_label, lv_color_make(0, 255, 0), LV_PART_MAIN);
    } else {
        lv_label_set_text(s_sc171_label, "SC171_disconnect");
        lv_obj_set_style_text_color(s_sc171_label, lv_color_make(255, 0, 0), LV_PART_MAIN);
    }
    bsp_display_unlock();
}

void ui_set_sc171_progress(int percent)
{
    if (percent < 0) percent = 0;
    if (percent > 100) percent = 100;
    bsp_display_lock(0);
    lv_obj_clear_flag(s_sc171_progress_label, LV_OBJ_FLAG_HIDDEN);
    lv_obj_clear_flag(s_sc171_progress_bar, LV_OBJ_FLAG_HIDDEN);
    lv_bar_set_value(s_sc171_progress_bar, percent, LV_ANIM_OFF);
    lv_label_set_text_fmt(s_sc171_progress_label, "SC171 %d%%", percent);
    lv_obj_move_foreground(s_sc171_progress_label);
    lv_obj_move_foreground(s_sc171_progress_bar);
    bsp_display_unlock();
}

void ui_set_pc_progress(int percent)
{
    if (percent < 0) percent = 0;
    if (percent > 100) percent = 100;
    bsp_display_lock(0);
    lv_obj_clear_flag(s_pc_progress_label, LV_OBJ_FLAG_HIDDEN);
    lv_obj_clear_flag(s_pc_progress_bar, LV_OBJ_FLAG_HIDDEN);
    lv_bar_set_value(s_pc_progress_bar, percent, LV_ANIM_OFF);
    lv_label_set_text_fmt(s_pc_progress_label, "PC %d%%", percent);
    lv_obj_move_foreground(s_pc_progress_label);
    lv_obj_move_foreground(s_pc_progress_bar);
    bsp_display_unlock();
}

void ui_hide_progress_bars(void)
{
    bsp_display_lock(0);
    lv_obj_add_flag(s_sc171_progress_label, LV_OBJ_FLAG_HIDDEN);
    lv_obj_add_flag(s_sc171_progress_bar, LV_OBJ_FLAG_HIDDEN);
    lv_obj_add_flag(s_pc_progress_label, LV_OBJ_FLAG_HIDDEN);
    lv_obj_add_flag(s_pc_progress_bar, LV_OBJ_FLAG_HIDDEN);
    bsp_display_unlock();
}

void ui_hide_media_image(void)
{
    bsp_display_lock(0);
    lv_obj_add_flag(s_media_image, LV_OBJ_FLAG_HIDDEN);
    bsp_display_unlock();
}

void ui_show_scoreboard(void)
{
    bsp_display_lock(0);
    s_return_requested = false;
    lv_obj_add_flag(s_media_root, LV_OBJ_FLAG_HIDDEN);
    lv_obj_clear_flag(s_score_root, LV_OBJ_FLAG_HIDDEN);
    bsp_display_unlock();
}

void ui_show_media(const char *status, bool show_return_button)
{
    bsp_display_lock(0);
    s_return_requested = false;
    lv_obj_add_flag(s_score_root, LV_OBJ_FLAG_HIDDEN);
    lv_obj_clear_flag(s_media_root, LV_OBJ_FLAG_HIDDEN);
    lv_obj_add_flag(s_media_image, LV_OBJ_FLAG_HIDDEN);
    lv_label_set_text(s_media_status, status != NULL ? status : "");
    if (show_return_button) {
        lv_obj_clear_flag(s_return_button, LV_OBJ_FLAG_HIDDEN);
    } else {
        lv_obj_add_flag(s_return_button, LV_OBJ_FLAG_HIDDEN);
    }
    /* 重置进度条 */
    lv_bar_set_value(s_sc171_progress_bar, 0, LV_ANIM_OFF);
    lv_bar_set_value(s_pc_progress_bar, 0, LV_ANIM_OFF);
    lv_obj_add_flag(s_sc171_progress_label, LV_OBJ_FLAG_HIDDEN);
    lv_obj_add_flag(s_sc171_progress_bar, LV_OBJ_FLAG_HIDDEN);
    lv_obj_add_flag(s_pc_progress_label, LV_OBJ_FLAG_HIDDEN);
    lv_obj_add_flag(s_pc_progress_bar, LV_OBJ_FLAG_HIDDEN);
    bsp_display_unlock();
}

esp_err_t ui_show_challenge_image(void)
{
    const uint8_t *data = challenge_jpg_start;
    size_t data_size = (size_t)(challenge_jpg_end - challenge_jpg_start);
    static const uint8_t ANIM_MAGIC[8] = {'P', '4', 'A', 'N', 'I', 'M', '1', '\0'};

    /* P4ANIM1 packed animation: loop all frames until return pressed */
    if (data_size >= 24 && memcmp(data, ANIM_MAGIC, sizeof(ANIM_MAGIC)) == 0) {
        const uint8_t *p = data + sizeof(ANIM_MAGIC);
        uint32_t width = (uint32_t)p[0] | ((uint32_t)p[1] << 8)
            | ((uint32_t)p[2] << 16) | ((uint32_t)p[3] << 24);
        uint32_t height = (uint32_t)p[4] | ((uint32_t)p[5] << 8)
            | ((uint32_t)p[6] << 16) | ((uint32_t)p[7] << 24);
        uint32_t frame_count = (uint32_t)p[8] | ((uint32_t)p[9] << 8)
            | ((uint32_t)p[10] << 16) | ((uint32_t)p[11] << 24);
        uint32_t delay_ms = (uint32_t)p[12] | ((uint32_t)p[13] << 8)
            | ((uint32_t)p[14] << 16) | ((uint32_t)p[15] << 24);
        p += 16;

        if (width != P4CONTROL_FRAME_WIDTH || height != P4CONTROL_FRAME_HEIGHT
            || frame_count == 0 || frame_count > 10000 || delay_ms < 10) {
            ESP_LOGE(TAG, "invalid P4ANIM1: %" PRIu32 "x%" PRIu32 ", %" PRIu32 " frames",
                     width, height, frame_count);
            ui_set_media_status("INVALID ANIMATION");
            return ESP_ERR_INVALID_RESPONSE;
        }

        ESP_LOGI(TAG, "P4ANIM1 loop: %" PRIu32 "x%" PRIu32 ", %" PRIu32 " frames, %" PRIu32 "ms",
                 width, height, frame_count, delay_ms);

        {
            const uint8_t *fp = p;
            for (uint32_t i = 0; i < frame_count; i++) {
                if (s_return_requested) break;  /* 只 peek，不消耗标志 */
                if (fp + 4 > data + data_size) break;
                uint32_t frame_size = (uint32_t)fp[0] | ((uint32_t)fp[1] << 8)
                    | ((uint32_t)fp[2] << 16) | ((uint32_t)fp[3] << 24);
                fp += 4;
                if (fp + frame_size > data + data_size) break;

                esp_err_t err = ui_decode_and_show_jpeg(fp, frame_size, i, frame_count);
                if (err != ESP_OK) return err;
                fp += frame_size;
                vTaskDelay(pdMS_TO_TICKS(delay_ms));
            }
        }
        return ESP_OK;
    }

    /* Fallback: single JPEG */
    ESP_LOGI(TAG, "showing embedded challenge image (%u bytes)", (unsigned)data_size);
    return ui_decode_and_show_jpeg(data, data_size, 0, 1);
}

void ui_set_media_status(const char *status)
{
    bsp_display_lock(0);
    lv_label_set_text(s_media_status, status != NULL ? status : "");
    if (status != NULL && status[0] != '\0') {
        lv_obj_clear_flag(s_media_status, LV_OBJ_FLAG_HIDDEN);
    } else {
        lv_obj_add_flag(s_media_status, LV_OBJ_FLAG_HIDDEN);
    }
    bsp_display_unlock();
}

void ui_set_return_visible(bool visible)
{
    bsp_display_lock(0);
    if (visible) {
        lv_obj_clear_flag(s_return_button, LV_OBJ_FLAG_HIDDEN);
    } else {
        lv_obj_add_flag(s_return_button, LV_OBJ_FLAG_HIDDEN);
    }
    bsp_display_unlock();
}

bool ui_take_return_requested(void)
{
    bool requested = s_return_requested;
    s_return_requested = false;
    return requested;
}

void ui_set_return_requested(void)
{
    s_return_requested = true;
}

esp_err_t ui_decode_and_show_jpeg(
    const uint8_t *jpeg_data,
    size_t jpeg_size,
    uint32_t index,
    uint32_t count
)
{
    if (jpeg_data == NULL || jpeg_size == 0 || jpeg_size > INT_MAX) {
        return ESP_ERR_INVALID_ARG;
    }

    unsigned slot = s_next_frame;
    jpeg_dec_config_t config = DEFAULT_JPEG_DEC_CONFIG();
    config.output_type = JPEG_PIXEL_FORMAT_RGB565_LE;
    jpeg_dec_handle_t decoder = NULL;
    jpeg_error_t jpeg_error = jpeg_dec_open(&config, &decoder);
    if (jpeg_error != JPEG_ERR_OK) {
        return ESP_FAIL;
    }

    jpeg_dec_io_t io = {
        .inbuf = (uint8_t *)jpeg_data,
        .inbuf_len = (int)jpeg_size,
        .outbuf = s_frame_pixels[slot],
    };
    jpeg_dec_header_info_t header = {0};
    jpeg_error = jpeg_dec_parse_header(decoder, &io, &header);
    if (jpeg_error == JPEG_ERR_OK
        && (header.width != P4CONTROL_FRAME_WIDTH || header.height != P4CONTROL_FRAME_HEIGHT)) {
        ESP_LOGE(TAG, "unexpected JPEG dimensions: %ux%u", header.width, header.height);
        jpeg_error = JPEG_ERR_INVALID_PARAM;
    }
    int output_size = 0;
    if (jpeg_error == JPEG_ERR_OK) {
        jpeg_error = jpeg_dec_get_outbuf_len(decoder, &output_size);
    }
    if (jpeg_error == JPEG_ERR_OK && output_size != (int)RGB565_FRAME_BYTES) {
        jpeg_error = JPEG_ERR_INVALID_PARAM;
    }
    if (jpeg_error == JPEG_ERR_OK) {
        jpeg_error = jpeg_dec_process(decoder, &io);
    }
    jpeg_dec_close(decoder);
    if (jpeg_error != JPEG_ERR_OK) {
        ESP_LOGE(TAG, "JPEG decode failed: %d", jpeg_error);
        return ESP_ERR_INVALID_RESPONSE;
    }

    bsp_display_lock(0);
    lv_image_set_src(s_media_image, &s_frame_images[slot]);
    lv_obj_center(s_media_image);
    lv_obj_clear_flag(s_media_image, LV_OBJ_FLAG_HIDDEN);
    lv_obj_add_flag(s_media_status, LV_OBJ_FLAG_HIDDEN);
    lv_obj_invalidate(s_media_image);
    lv_refr_now(s_display);
    bsp_display_unlock();

    s_next_frame ^= 1U;
    if (index == 0 || index + 1 == count || (index + 1) % 30 == 0) {
        ESP_LOGI(TAG, "displayed frame %" PRIu32 "/%" PRIu32, index + 1, count);
    }
    return ESP_OK;
}
