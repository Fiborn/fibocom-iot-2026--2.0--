#include <stdbool.h>
#include <stdio.h>
#include <string.h>

#include "cloud_player.h"
#include "esp_err.h"
#include "esp_log.h"
#include "esp_system.h"
#include "freertos/FreeRTOS.h"
#include "freertos/queue.h"
#include "freertos/task.h"
#include "nvs_flash.h"
#include "sc171_udp.h"
#include "sdkconfig.h"
#include "ui.h"
#include "wifi_manager.h"

static const char *TAG = "p4control";
static QueueHandle_t s_actions;
static QueueHandle_t s_challenges;

static void initialize_nvs(void)
{
    esp_err_t err = nvs_flash_init();
    if (err == ESP_ERR_NVS_NO_FREE_PAGES || err == ESP_ERR_NVS_NEW_VERSION_FOUND) {
        ESP_ERROR_CHECK(nvs_flash_erase());
        err = nvs_flash_init();
    }
    ESP_ERROR_CHECK(err);
}

static void action_from_ui(ui_action_t action)
{
    if (s_actions != NULL) {
        xQueueSend(s_actions, &action, 0);
    }
}

static int media_pc_progress_from_status(const cloud_player_status_t *status)
{
    switch (status->state) {
    case CLOUD_PLAYER_STATE_IDLE:
    case CLOUD_PLAYER_STATE_WAITING_WIFI:
    case CLOUD_PLAYER_STATE_SYNCING_TIME:
        return 0;
    case CLOUD_PLAYER_STATE_FETCHING_MANIFEST:
        return 5;
    case CLOUD_PLAYER_STATE_WAITING_NEW_GENERATION:
        return 50;  /* PC is rendering the video */
    case CLOUD_PLAYER_STATE_DOWNLOADING:
        return status->total_frames > 0
            ? 50 + status->downloaded_frames * 50 / status->total_frames
            : 50;
    case CLOUD_PLAYER_STATE_READY:
        return 100;
    case CLOUD_PLAYER_STATE_ERROR:
        return 50;  /* retrying, stay at current */
    default:
        return 0;
    }
}

static void run_challenge(void)
{
    ui_show_media("", true);

    esp_err_t start_err = cloud_player_start();
    if (start_err != ESP_OK) {
        ESP_LOGE(TAG, "cloud player start failed: %s", esp_err_to_name(start_err));
        ui_show_challenge_image();
        ui_set_media_status("CACHE TASK START FAILED");
        vTaskDelay(pdMS_TO_TICKS(3000));
        ui_show_scoreboard();
        return;
    }

    /* 循环播内置动画，同时等待云帧就绪。
       云帧下载由 cloud_player 后台 loader_task 处理。
       每个动画周期结束后检查 cloud ready + 返回按钮 + 更新 PC 进度条。 */
    while (true) {
        if (cloud_player_wait_ready(0) == ESP_OK) {
            break;
        }
        if (ui_take_return_requested()) {
            cloud_player_stop();
            ui_show_scoreboard();
            return;
        }
        /* 更新 PC 进度条：只在实际传输帧时显示 */
        cloud_player_status_t ps = {0};
        cloud_player_get_status(&ps);
        if (ps.state == CLOUD_PLAYER_STATE_DOWNLOADING
            || ps.state == CLOUD_PLAYER_STATE_READY) {
            int pc_progress = media_pc_progress_from_status(&ps);
            if (pc_progress >= 0) {
                ui_set_pc_progress(pc_progress);
            }
        }

        /* 播完一整遍动画，然后回到循环开头重新检查 */
        ui_show_challenge_image();
    }

    ui_set_pc_progress(100);
    ui_set_media_status("");
    /* 隐藏等待阶段的进度条和动画，只显示 PC 传过来的判定录像 */
    ui_hide_progress_bars();
    ui_hide_media_image();
    esp_err_t err = cloud_player_play_until_return();
    if (err != ESP_OK) {
        ESP_LOGE(TAG, "cloud playback failed: %s", esp_err_to_name(err));
    }
    cloud_player_stop();
    ui_show_scoreboard();
}

static void challenge_task(void *argument)
{
    (void)argument;
    ui_action_t action;
    while (true) {
        if (xQueueReceive(s_challenges, &action, portMAX_DELAY) != pdTRUE) {
            continue;
        }
        if (action == UI_ACTION_CHALLENGE_A_TO_B) {
            run_challenge();
        } else if (action == UI_ACTION_CHALLENGE_B_TO_A) {
            run_challenge();
        }
    }
}

static void status_update_task(void *argument)
{
    (void)argument;
    bool prev_wifi = false;
    bool prev_sc171 = false;
    while (true) {
        bool wifi_ok = wifi_manager_is_connected();
        bool sc171_ok = sc171_udp_is_connected();
        if (wifi_ok != prev_wifi) {
            ui_set_wifi_status(wifi_ok);
            prev_wifi = wifi_ok;
        }
        if (sc171_ok != prev_sc171) {
            ui_set_sc171_status(sc171_ok);
            prev_sc171 = sc171_ok;
        }
        vTaskDelay(pdMS_TO_TICKS(2000));
    }
}

static void network_task(void *argument)
{
    (void)argument;
    unsigned attempt = 0;
    while (true) {
        ++attempt;
        ESP_LOGI(TAG, "initializing ESP32-C6 Wi-Fi in background (attempt %u)", attempt);
        esp_err_t wifi_err = wifi_manager_init();
        if (wifi_err == ESP_OK) {
            ESP_LOGI(TAG, "ESP32-C6 transport initialized; waiting for Wi-Fi IPv4 address");
            vTaskDelete(NULL);
            return;
        }
        ESP_LOGE(
            TAG,
            "ESP32-C6 Wi-Fi initialization attempt %u failed: %s; retrying",
            attempt,
            esp_err_to_name(wifi_err)
        );
        vTaskDelay(pdMS_TO_TICKS(attempt < 3 ? 3000 : 10000));
    }
}

void app_main(void)
{
    ESP_LOGW(TAG, "boot reset reason=%d", (int)esp_reset_reason());
    initialize_nvs();
    s_actions = xQueueCreate(8, sizeof(ui_action_t));
    ESP_ERROR_CHECK(s_actions != NULL ? ESP_OK : ESP_ERR_NO_MEM);
    s_challenges = xQueueCreate(1, sizeof(ui_action_t));
    ESP_ERROR_CHECK(s_challenges != NULL ? ESP_OK : ESP_ERR_NO_MEM);
    ESP_ERROR_CHECK(ui_init(action_from_ui));
    ESP_ERROR_CHECK(sc171_udp_init());

    BaseType_t challenge_created = xTaskCreate(
        challenge_task,
        "challenge",
        8192,
        NULL,
        5,
        NULL
    );
    ESP_ERROR_CHECK(challenge_created == pdPASS ? ESP_OK : ESP_ERR_NO_MEM);

    BaseType_t status_created = xTaskCreate(
        status_update_task,
        "status_update",
        4096,
        NULL,
        4,
        NULL
    );
    ESP_ERROR_CHECK(status_created == pdPASS ? ESP_OK : ESP_ERR_NO_MEM);

    BaseType_t network_created = xTaskCreate(
        network_task,
        "network_init",
        8192,
        NULL,
        5,
        NULL
    );
    ESP_ERROR_CHECK(network_created == pdPASS ? ESP_OK : ESP_ERR_NO_MEM);

    ESP_LOGI(TAG, "UI event loop ready");
    ui_action_t action;
    while (true) {
        if (xQueueReceive(s_actions, &action, portMAX_DELAY) != pdTRUE) {
            continue;
        }
        switch (action) {
        case UI_ACTION_ROUND_START:
            ESP_ERROR_CHECK_WITHOUT_ABORT(sc171_udp_send('a'));
            break;
        case UI_ACTION_ROUND_END:
            ESP_ERROR_CHECK_WITHOUT_ABORT(sc171_udp_send('b'));
            break;
        case UI_ACTION_CHALLENGE_A_TO_B:
        case UI_ACTION_CHALLENGE_B_TO_A:
            ESP_ERROR_CHECK_WITHOUT_ABORT(sc171_udp_send('c'));
            if (xQueueSend(s_challenges, &action, 0) != pdTRUE) {
                ESP_LOGW(TAG, "a challenge is already being displayed");
            }
            break;
        }
    }
}
