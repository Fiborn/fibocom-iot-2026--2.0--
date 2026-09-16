#include "animation_player.h"

#include <stdio.h>
#include <string.h>

#include "esp_heap_caps.h"
#include "esp_log.h"
#include "esp_spiffs.h"
#include "esp_timer.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "sdkconfig.h"
#include "ui.h"

static const char *TAG = "animation";
static const uint8_t PACK_MAGIC[8] = {'P', '4', 'A', 'N', 'I', 'M', '1', '\0'};

static const char *resolve_storage_path(
    const char *configured_path,
    char *resolved_path,
    size_t resolved_size
)
{
    if (configured_path == NULL || configured_path[0] == '\0') {
        return configured_path;
    }
    if (strncmp(configured_path, "/storage/", strlen("/storage/")) == 0) {
        return configured_path;
    }

    const char *name = strrchr(configured_path, '/');
    const char *windows_name = strrchr(configured_path, '\\');
    if (windows_name != NULL && (name == NULL || windows_name > name)) {
        name = windows_name;
    }
    name = name != NULL ? name + 1 : configured_path;
    int written = snprintf(resolved_path, resolved_size, "/storage/%s", name);
    if (name[0] == '\0' || written < 0 || (size_t)written >= resolved_size) {
        return configured_path;
    }

    ESP_LOGW(
        TAG,
        "animation path '%s' is not an ESP32 path; using '%s'",
        configured_path,
        resolved_path
    );
    return resolved_path;
}

static bool read_exact(FILE *file, void *buffer, size_t length)
{
    return fread(buffer, 1, length, file) == length;
}

static bool read_u32_le(FILE *file, uint32_t *value)
{
    uint8_t bytes[4];
    if (!read_exact(file, bytes, sizeof(bytes))) {
        return false;
    }
    *value = (uint32_t)bytes[0]
        | ((uint32_t)bytes[1] << 8)
        | ((uint32_t)bytes[2] << 16)
        | ((uint32_t)bytes[3] << 24);
    return true;
}

esp_err_t animation_player_init(void)
{
    int64_t started_us = esp_timer_get_time();
    ESP_LOGI(TAG, "mounting SPIFFS animation storage");
    esp_vfs_spiffs_conf_t config = {
        .base_path = "/storage",
        .partition_label = "storage",
        .max_files = 4,
        .format_if_mount_failed = false,
    };
    esp_err_t err = esp_vfs_spiffs_register(&config);
    if (err == ESP_ERR_INVALID_STATE) {
        return ESP_OK;
    }
    if (err != ESP_OK) {
        ESP_LOGW(TAG, "SPIFFS mount failed: %s", esp_err_to_name(err));
    } else {
        size_t total_bytes = 0;
        size_t used_bytes = 0;
        esp_err_t info_err = esp_spiffs_info("storage", &total_bytes, &used_bytes);
        if (info_err == ESP_OK) {
            ESP_LOGI(
                TAG,
                "SPIFFS ready in %lld ms: %u/%u bytes used",
                (long long)((esp_timer_get_time() - started_us) / 1000),
                (unsigned)used_bytes,
                (unsigned)total_bytes
            );
        }
    }
    return err;
}

esp_err_t animation_player_play(const char *path)
{
    if (path == NULL || path[0] == '\0') {
        return ESP_ERR_INVALID_ARG;
    }
    char resolved_path[128];
    path = resolve_storage_path(path, resolved_path, sizeof(resolved_path));
    FILE *file = fopen(path, "rb");
    if (file == NULL) {
        ESP_LOGW(TAG, "local animation is missing: %s", path);
        ui_set_media_status("LOCAL ANIMATION NOT FOUND");
        vTaskDelay(pdMS_TO_TICKS(CONFIG_APP_MISSING_ANIMATION_DELAY_MS));
        return ESP_ERR_NOT_FOUND;
    }

    uint8_t magic[8];
    uint32_t width = 0;
    uint32_t height = 0;
    uint32_t frame_count = 0;
    uint32_t delay_ms = 0;
    bool valid_header = read_exact(file, magic, sizeof(magic))
        && memcmp(magic, PACK_MAGIC, sizeof(magic)) == 0
        && read_u32_le(file, &width)
        && read_u32_le(file, &height)
        && read_u32_le(file, &frame_count)
        && read_u32_le(file, &delay_ms);
    if (!valid_header || width != P4CONTROL_FRAME_WIDTH || height != P4CONTROL_FRAME_HEIGHT
        || frame_count == 0 || frame_count > 10000 || delay_ms < 20 || delay_ms > 60000) {
        fclose(file);
        ui_set_media_status("INVALID LOCAL ANIMATION");
        vTaskDelay(pdMS_TO_TICKS(CONFIG_APP_MISSING_ANIMATION_DELAY_MS));
        return ESP_ERR_INVALID_RESPONSE;
    }

    uint8_t *jpeg = NULL;
    size_t capacity = 0;
    int64_t next_frame_us = esp_timer_get_time();
    esp_err_t result = ESP_OK;
    for (uint32_t index = 0; index < frame_count; ++index) {
        uint32_t frame_size = 0;
        if (!read_u32_le(file, &frame_size) || frame_size == 0
            || frame_size > CONFIG_APP_MAX_JPEG_BYTES) {
            result = ESP_ERR_INVALID_SIZE;
            break;
        }
        if (frame_size > capacity) {
            uint8_t *next = heap_caps_realloc(
                jpeg,
                frame_size,
                MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT
            );
            if (next == NULL) {
                result = ESP_ERR_NO_MEM;
                break;
            }
            jpeg = next;
            capacity = frame_size;
        }
        if (!read_exact(file, jpeg, frame_size)) {
            result = ESP_ERR_INVALID_SIZE;
            break;
        }
        result = ui_decode_and_show_jpeg(jpeg, frame_size, index, frame_count);
        if (result != ESP_OK) {
            break;
        }

        next_frame_us += (int64_t)delay_ms * 1000;
        int64_t remaining_us = next_frame_us - esp_timer_get_time();
        if (remaining_us > 0) {
            vTaskDelay(pdMS_TO_TICKS((remaining_us + 999) / 1000));
        } else {
            next_frame_us = esp_timer_get_time();
        }
    }

    heap_caps_free(jpeg);
    fclose(file);
    if (result != ESP_OK) {
        ESP_LOGE(TAG, "animation %s failed: %s", path, esp_err_to_name(result));
    }
    return result;
}
