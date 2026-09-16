#include "cloud_player.h"

#include <inttypes.h>
#include <stdbool.h>
#include <string.h>

#include "esp_check.h"
#include "esp_heap_caps.h"
#include "esp_log.h"
#include "esp_timer.h"
#include "freertos/FreeRTOS.h"
#include "freertos/event_groups.h"
#include "freertos/task.h"
#include "http_frames.h"
#include "sdkconfig.h"
#include "time_sync.h"
#include "ui.h"
#include "wifi_manager.h"

static const char *TAG = "cloud_player";
static const EventBits_t CACHE_READY_BIT = BIT0;
static const EventBits_t CACHE_STOP_BIT = BIT1;
static const EventBits_t CACHE_POLL_NOW_BIT = BIT2;
static portMUX_TYPE s_status_lock = portMUX_INITIALIZER_UNLOCKED;

#define FRAME_RECOVERY_CYCLES 5
#define FRAME_REQUEST_GAP_MS  30

typedef struct {
    uint8_t *data;
    size_t size;
} cached_frame_t;

typedef struct {
    cached_frame_t *frames;
    uint32_t count;
    uint32_t delay_ms;
    size_t jpeg_bytes;
} frame_cache_t;

static EventGroupHandle_t s_events;
static frame_cache_t s_cache;
static TaskHandle_t s_loader_task;
static char s_last_generation[65];
static cloud_player_status_t s_status = {
    .state = CLOUD_PLAYER_STATE_IDLE,
    .last_error = ESP_OK,
};

static void set_status(
    cloud_player_state_t state,
    uint32_t downloaded_frames,
    uint32_t total_frames,
    esp_err_t last_error
)
{
    taskENTER_CRITICAL(&s_status_lock);
    s_status = (cloud_player_status_t) {
        .state = state,
        .downloaded_frames = downloaded_frames,
        .total_frames = total_frames,
        .last_error = last_error,
    };
    taskEXIT_CRITICAL(&s_status_lock);
}

static esp_err_t wait_for_new_manifest(
    const char *after_generation,
    frame_manifest_t *manifest
)
{
    while (true) {
        esp_err_t err = wifi_manager_wait_connected(30000);
        if (err != ESP_OK) {
            set_status(CLOUD_PLAYER_STATE_WAITING_WIFI, 0, 0, err);
            vTaskDelay(pdMS_TO_TICKS(1000));
            continue;
        }
        set_status(CLOUD_PLAYER_STATE_WAITING_NEW_GENERATION, 0, 0, ESP_OK);
        err = http_frames_fetch_manifest_after(after_generation, manifest);
        if (err == ESP_OK) {
            return ESP_OK;
        }
        /*
         * HTTP 连接失败通常意味着服务端未就绪，不是 WiFi 问题。
         * 不要触发 WiFi 重连，仅等待后重试。
         */
        if (err != ESP_ERR_INVALID_STATE) {
            ESP_LOGW(TAG, "manifest poll failed: %s (retrying)", esp_err_to_name(err));
        }
        /*
         * A 409 means the render job is still producing the same generation.
         * Five-second polling keeps the server terminal readable while adding
         * at most five seconds before playback begins.
         *
         * Wait interruptibly: FRAMES_READY UDP from PC wakes us immediately.
         */
        xEventGroupWaitBits(
            s_events,
            CACHE_POLL_NOW_BIT | CACHE_STOP_BIT,
            pdTRUE,   /* 自动清除触发 bit */
            pdFALSE,  /* 任一 bit 即可 */
            pdMS_TO_TICKS(5000)
        );
    }
}

static esp_err_t fetch_frame_with_retry(
    const frame_manifest_t *manifest,
    uint32_t index,
    uint8_t **jpeg_data,
    size_t *jpeg_size
)
{
    esp_err_t err = ESP_FAIL;
    for (int attempt = 1; attempt <= CONFIG_APP_DOWNLOAD_RETRIES; ++attempt) {
        err = wifi_manager_wait_connected(30000);
        if (err == ESP_OK) {
            err = http_frames_fetch_frame(index, manifest->generation, jpeg_data, jpeg_size);
        }
        if (err == ESP_OK || err == ESP_ERR_INVALID_STATE) {
            return err;
        }
        ESP_LOGW(
            TAG,
            "frame %" PRIu32 "/%" PRIu32 " request attempt %d/%d failed: %s; "
            "free PSRAM=%u",
            index + 1,
            manifest->count,
            attempt,
            CONFIG_APP_DOWNLOAD_RETRIES,
            esp_err_to_name(err),
            (unsigned)heap_caps_get_free_size(MALLOC_CAP_SPIRAM)
        );
        if (err != ESP_ERR_INVALID_STATE && err != ESP_ERR_NO_MEM) {
            esp_err_t reconnect_err = wifi_manager_reconnect();
            if (reconnect_err != ESP_OK) {
                ESP_LOGW(
                    TAG,
                    "Wi-Fi recovery after frame failure did not start: %s",
                    esp_err_to_name(reconnect_err)
                );
            }
        }
        vTaskDelay(pdMS_TO_TICKS(500 * attempt));
    }
    return err;
}

static void cache_release(frame_cache_t *cache)
{
    if (cache == NULL) {
        return;
    }
    for (uint32_t index = 0; index < cache->count; ++index) {
        http_frames_free(cache->frames[index].data);
    }
    heap_caps_free(cache->frames);
    memset(cache, 0, sizeof(*cache));
}

static esp_err_t cache_preload(const frame_manifest_t *manifest, frame_cache_t *cache)
{
    memset(cache, 0, sizeof(*cache));
    cache->frames = heap_caps_calloc(
        manifest->count,
        sizeof(*cache->frames),
        MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT
    );
    if (cache->frames == NULL) {
        return ESP_ERR_NO_MEM;
    }
    cache->count = manifest->count;
    cache->delay_ms = CONFIG_APP_FRAME_DELAY_OVERRIDE_MS > 0
        ? CONFIG_APP_FRAME_DELAY_OVERRIDE_MS
        : manifest->delay_ms;
    set_status(CLOUD_PLAYER_STATE_DOWNLOADING, 0, manifest->count, ESP_OK);

    for (uint32_t index = 0; index < manifest->count; ++index) {
        esp_err_t err = ESP_FAIL;
        for (int recovery = 1; recovery <= FRAME_RECOVERY_CYCLES; ++recovery) {
            err = fetch_frame_with_retry(
                manifest,
                index,
                &cache->frames[index].data,
                &cache->frames[index].size
            );
            if (err == ESP_OK) {
                break;
            }

            set_status(
                CLOUD_PLAYER_STATE_ERROR,
                index,
                manifest->count,
                err
            );
            ESP_LOGE(
                TAG,
                "frame %" PRIu32 "/%" PRIu32 " recovery cycle %d/%d failed: %s",
                index + 1,
                manifest->count,
                recovery,
                FRAME_RECOVERY_CYCLES,
                esp_err_to_name(err)
            );
            if (err == ESP_ERR_INVALID_STATE || err == ESP_ERR_NO_MEM) {
                break;
            }
            vTaskDelay(pdMS_TO_TICKS(2000));
            set_status(
                CLOUD_PLAYER_STATE_DOWNLOADING,
                index,
                manifest->count,
                ESP_OK
            );
        }
        if (err != ESP_OK) {
            cache_release(cache);
            return err;
        }
        cache->jpeg_bytes += cache->frames[index].size;
        set_status(CLOUD_PLAYER_STATE_DOWNLOADING, index + 1, manifest->count, ESP_OK);
        if (index == 0 || index + 1 == manifest->count || (index + 1) % 10 == 0) {
            ESP_LOGI(TAG, "cached %" PRIu32 "/%" PRIu32, index + 1, manifest->count);
        }
        if (index + 1 < manifest->count) {
            /*
             * Give ESP-Hosted/C6 and the phone hotspot a short scheduling gap
             * between many consecutive HTTP connections.
             */
            vTaskDelay(pdMS_TO_TICKS(FRAME_REQUEST_GAP_MS));
        }
    }
    ESP_LOGI(
        TAG,
        "cloud cache ready: %" PRIu32 " frames, %.2f MB",
        cache->count,
        (double)cache->jpeg_bytes / (1024.0 * 1024.0)
    );
    return ESP_OK;
}

static void loader_task(void *argument)
{
    (void)argument;
    char baseline_generation[65] = {0};
    strlcpy(
        baseline_generation,
        s_last_generation,
        sizeof(baseline_generation)
    );

    esp_err_t err = ESP_FAIL;
    while (err != ESP_OK) {
        if (xEventGroupGetBits(s_events) & CACHE_STOP_BIT) {
            ESP_LOGI(TAG, "loader aborted before wifi connect");
            s_loader_task = NULL;
            vTaskDelete(NULL);
        }
        set_status(CLOUD_PLAYER_STATE_WAITING_WIFI, 0, 0, ESP_OK);
        err = wifi_manager_wait_connected(UINT32_MAX);
        if (err == ESP_OK
            && strncmp(CONFIG_APP_SERVER_BASE_URL, "https://", 8) == 0) {
            set_status(CLOUD_PLAYER_STATE_SYNCING_TIME, 0, 0, ESP_OK);
            err = time_sync_for_tls();
        }
        if (err != ESP_OK) {
            set_status(CLOUD_PLAYER_STATE_ERROR, 0, 0, err);
            vTaskDelay(pdMS_TO_TICKS(3000));
        }
    }

    /*
     * On the first challenge after boot, remember the server's currently
     * published generation as a baseline. The SC171-triggered dual-video task
     * takes much longer than this request, so the loader can safely wait for a
     * different generation and never replay a stale result.
     */
    if (err == ESP_OK && baseline_generation[0] == '\0') {
        int baseline_retries = 0;
        while (true) {
            if (xEventGroupGetBits(s_events) & CACHE_STOP_BIT) {
                ESP_LOGI(TAG, "loader aborted during baseline fetch");
                s_loader_task = NULL;
                vTaskDelete(NULL);
            }
            set_status(CLOUD_PLAYER_STATE_FETCHING_MANIFEST, 0, 0, ESP_OK);
            frame_manifest_t current = {0};
            err = http_frames_fetch_manifest(&current);
            if (err == ESP_OK) {
                strlcpy(
                    baseline_generation,
                    current.generation,
                    sizeof(baseline_generation)
                );
                ESP_LOGI(
                    TAG,
                    "baseline generation %.12s; waiting for challenge result",
                    baseline_generation
                );
                break;
            }
            if (err == ESP_ERR_INVALID_STATE) {
                err = ESP_OK;
                ESP_LOGI(TAG, "server has no previous generation; waiting for first result");
                break;
            }
            set_status(CLOUD_PLAYER_STATE_ERROR, 0, 0, err);
            ESP_LOGW(TAG, "baseline manifest failed: %s (retry %d)",
                     esp_err_to_name(err), ++baseline_retries);
            /* HTTP 连接失败不是 WiFi 问题，不触发重连，仅等待重试 */
            vTaskDelay(pdMS_TO_TICKS(5000));
        }
    }

    while (true) {
        if (xEventGroupGetBits(s_events) & CACHE_STOP_BIT) {
            ESP_LOGI(TAG, "loader aborted");
            cache_release(&s_cache);
            s_loader_task = NULL;
            vTaskDelete(NULL);
        }
        frame_manifest_t manifest = {0};
        if (err == ESP_OK) {
            err = wait_for_new_manifest(baseline_generation, &manifest);
        }
        if (err == ESP_OK
            && (manifest.width != P4CONTROL_FRAME_WIDTH
                || manifest.height != P4CONTROL_FRAME_HEIGHT)) {
            err = ESP_ERR_INVALID_SIZE;
        }
        frame_cache_t cache = {0};
        if (err == ESP_OK) {
            err = cache_preload(&manifest, &cache);
        }
        if (err == ESP_OK) {
            s_cache = cache;
            strlcpy(
                s_last_generation,
                manifest.generation,
                sizeof(s_last_generation)
            );
            set_status(CLOUD_PLAYER_STATE_READY, cache.count, cache.count, ESP_OK);
            xEventGroupSetBits(s_events, CACHE_READY_BIT);
            s_loader_task = NULL;
            vTaskDelete(NULL);
        }
        cache_release(&cache);
        cloud_player_status_t failure_status = {0};
        cloud_player_get_status(&failure_status);
        if (failure_status.state != CLOUD_PLAYER_STATE_ERROR) {
            set_status(CLOUD_PLAYER_STATE_ERROR, 0, manifest.count, err);
        }
        ESP_LOGE(TAG, "cloud preload failed: %s; retrying", esp_err_to_name(err));
        vTaskDelay(pdMS_TO_TICKS(5000));
        err = ESP_OK;
    }
}

esp_err_t cloud_player_start(void)
{
    if (s_loader_task != NULL) {
        return ESP_ERR_INVALID_STATE;
    }
    if (s_events == NULL) {
        s_events = xEventGroupCreate();
        if (s_events == NULL) {
            return ESP_ERR_NO_MEM;
        }
    }
    xEventGroupClearBits(s_events, CACHE_READY_BIT | CACHE_STOP_BIT | CACHE_POLL_NOW_BIT);
    cache_release(&s_cache);
    set_status(CLOUD_PLAYER_STATE_IDLE, 0, 0, ESP_OK);
    BaseType_t created = xTaskCreate(
        loader_task,
        "cloud_loader",
        8192,
        NULL,
        5,
        &s_loader_task
    );
    if (created != pdPASS) {
        s_loader_task = NULL;
        return ESP_ERR_NO_MEM;
    }
    return ESP_OK;
}

void cloud_player_get_status(cloud_player_status_t *status)
{
    if (status == NULL) {
        return;
    }
    taskENTER_CRITICAL(&s_status_lock);
    *status = s_status;
    taskEXIT_CRITICAL(&s_status_lock);
}

esp_err_t cloud_player_wait_ready(uint32_t timeout_ms)
{
    if (s_events == NULL) {
        return ESP_ERR_INVALID_STATE;
    }
    TickType_t ticks = timeout_ms == UINT32_MAX ? portMAX_DELAY : pdMS_TO_TICKS(timeout_ms);
    EventBits_t bits = xEventGroupWaitBits(s_events, CACHE_READY_BIT, pdFALSE, pdTRUE, ticks);
    return (bits & CACHE_READY_BIT) != 0 ? ESP_OK : ESP_ERR_TIMEOUT;
}

esp_err_t cloud_player_play_until_return(void)
{
    ESP_RETURN_ON_ERROR(cloud_player_wait_ready(0), TAG, "cloud cache is not ready");
    if (s_cache.frames == NULL || s_cache.count == 0 || s_cache.delay_ms == 0) {
        return ESP_ERR_INVALID_STATE;
    }

    /* 循环播放直到按返回键 */
    while (!ui_take_return_requested()) {
        int64_t next_frame_us = esp_timer_get_time();
        for (uint32_t index = 0; index < s_cache.count; ++index) {
            if (ui_take_return_requested()) {
                return ESP_OK;
            }
            ESP_RETURN_ON_ERROR(
                ui_decode_and_show_jpeg(
                    s_cache.frames[index].data,
                    s_cache.frames[index].size,
                    index,
                    s_cache.count
                ),
                TAG,
                "cloud frame display failed"
            );

            next_frame_us += (int64_t)s_cache.delay_ms * 1000;
            int64_t remaining_us = next_frame_us - esp_timer_get_time();
            /* 确保至少 1 tick 延迟，让 LVGL 任务有时间处理输入事件（返回按钮等） */
            TickType_t y_ticks = 1;
            if (remaining_us > 0) {
                y_ticks = pdMS_TO_TICKS((remaining_us + 999) / 1000);
                if (y_ticks == 0) y_ticks = 1;
            }
            vTaskDelay(y_ticks);
        }
    }
    return ESP_OK;
}

void cloud_player_stop(void)
{
    if (s_events != NULL) {
        xEventGroupSetBits(s_events, CACHE_STOP_BIT);
    }
    /* 等待 loader task 退出 */
    for (int retry = 0; retry < 100; ++retry) {
        if (s_loader_task == NULL) {
            break;
        }
        vTaskDelay(pdMS_TO_TICKS(10));
    }
    if (s_loader_task != NULL) {
        ESP_LOGW(TAG, "loader task did not stop within 1s; killing");
        vTaskDelete(s_loader_task);
        s_loader_task = NULL;
    }
    cache_release(&s_cache);
    set_status(CLOUD_PLAYER_STATE_IDLE, 0, 0, ESP_OK);
}

void cloud_player_request_poll(void)
{
    if (s_events != NULL) {
        xEventGroupSetBits(s_events, CACHE_POLL_NOW_BIT);
    }
}
