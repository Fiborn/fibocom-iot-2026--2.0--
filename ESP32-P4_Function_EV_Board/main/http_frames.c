#include "http_frames.h"

#include <ctype.h>
#include <inttypes.h>
#include <stdbool.h>
#include <stdio.h>
#include <string.h>

#include "cJSON.h"
#include "esp_check.h"
#include "esp_crt_bundle.h"
#include "esp_heap_caps.h"
#include "esp_http_client.h"
#include "esp_log.h"
#include "sdkconfig.h"

static const char *TAG = "http_frames";

typedef struct {
    uint8_t *data;
    size_t length;
    size_t capacity;
    size_t limit;
    esp_err_t error;
} response_buffer_t;

static esp_err_t reserve_response(response_buffer_t *response, size_t required)
{
    if (required <= response->capacity) {
        return ESP_OK;
    }
    size_t next_capacity = response->capacity == 0 ? 4096 : response->capacity;
    while (next_capacity < required) {
        next_capacity *= 2;
    }
    if (next_capacity > response->limit + 1) {
        next_capacity = response->limit + 1;
    }
    if (next_capacity < required) {
        return ESP_ERR_NO_MEM;
    }

    uint8_t *next = response->data == NULL
        ? heap_caps_malloc(next_capacity, MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT)
        : heap_caps_realloc(
            response->data,
            next_capacity,
            MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT
        );
    if (next == NULL) {
        return ESP_ERR_NO_MEM;
    }
    response->data = next;
    response->capacity = next_capacity;
    return ESP_OK;
}

static esp_err_t http_event_handler(esp_http_client_event_t *event)
{
    response_buffer_t *response = (response_buffer_t *)event->user_data;
    if (event->event_id != HTTP_EVENT_ON_DATA || event->data_len <= 0 || response == NULL) {
        return ESP_OK;
    }
    if (response->error != ESP_OK) {
        return response->error;
    }
    size_t incoming = (size_t)event->data_len;
    if (incoming > response->limit - response->length) {
        response->error = ESP_ERR_NO_MEM;
        return response->error;
    }
    response->error = reserve_response(response, response->length + incoming + 1);
    if (response->error != ESP_OK) {
        return response->error;
    }
    memcpy(response->data + response->length, event->data, incoming);
    response->length += incoming;
    response->data[response->length] = '\0';
    return ESP_OK;
}

static esp_err_t build_url(const char *path, char *url, size_t url_size)
{
    const char *base = CONFIG_APP_SERVER_BASE_URL;
    size_t base_length = strlen(base);
    bool base_has_slash = base_length > 0 && base[base_length - 1] == '/';
    bool path_has_slash = path[0] == '/';
    int written = snprintf(
        url,
        url_size,
        "%s%s%s",
        base,
        (base_has_slash || path_has_slash) ? "" : "/",
        (base_has_slash && path_has_slash) ? path + 1 : path
    );
    return written > 0 && (size_t)written < url_size ? ESP_OK : ESP_ERR_INVALID_SIZE;
}

static esp_err_t http_get_alloc(
    const char *path,
    const char *accept,
    size_t limit,
    uint8_t **body,
    size_t *body_length
)
{
    char url[512];
    ESP_RETURN_ON_ERROR(build_url(path, url, sizeof(url)), TAG, "URL is too long");
    response_buffer_t response = {
        .limit = limit,
        .error = ESP_OK,
    };
    esp_http_client_config_t config = {
        .url = url,
        .event_handler = http_event_handler,
        .user_data = &response,
        .timeout_ms = CONFIG_APP_HTTP_TIMEOUT_MS,
        .buffer_size = 4096,
        .buffer_size_tx = 1024,
        .crt_bundle_attach = (strncmp(url, "https://", 8) == 0)
                           ? esp_crt_bundle_attach : NULL,
        .keep_alive_enable = true,
        .user_agent = "P4Control/1.0 ESP32-P4",
    };
    esp_http_client_handle_t client = esp_http_client_init(&config);
    if (client == NULL) {
        return ESP_ERR_NO_MEM;
    }

    char authorization[256];
    int auth_length = 0;
    if (CONFIG_APP_BEARER_TOKEN[0] != '\0') {
        auth_length = snprintf(
            authorization,
            sizeof(authorization),
            "Bearer %s",
            CONFIG_APP_BEARER_TOKEN
        );
        if (auth_length <= 0 || (size_t)auth_length >= sizeof(authorization)) {
            esp_http_client_cleanup(client);
            return ESP_ERR_INVALID_SIZE;
        }
        esp_http_client_set_header(client, "Authorization", authorization);
    }
    esp_http_client_set_header(client, "Accept", accept);

    esp_err_t err = esp_http_client_perform(client);
    int status_code = esp_http_client_get_status_code(client);
    int64_t declared_length = esp_http_client_get_content_length(client);
    esp_http_client_cleanup(client);
    ESP_LOGI(
        TAG,
        "GET %s -> HTTP %d, declared=%lld, received=%u",
        url,
        status_code,
        (long long)declared_length,
        (unsigned)response.length
    );
    if (err == ESP_OK && response.error != ESP_OK) {
        err = response.error;
    }
    if (err != ESP_OK) {
        ESP_LOGE(TAG, "GET %s failed: %s", url, esp_err_to_name(err));
        heap_caps_free(response.data);
        return err;
    }
    if (status_code != 200) {
        if (status_code == 409) {
            ESP_LOGI(TAG, "GET %s: cloud result is not ready yet (HTTP 409)", url);
        } else {
            ESP_LOGE(TAG, "GET %s returned HTTP %d", url, status_code);
        }
        heap_caps_free(response.data);
        return status_code == 409 ? ESP_ERR_INVALID_STATE : ESP_ERR_INVALID_RESPONSE;
    }
    if (response.length == 0) {
        heap_caps_free(response.data);
        return ESP_ERR_INVALID_SIZE;
    }

    uint8_t *trimmed = heap_caps_realloc(
        response.data,
        response.length,
        MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT
    );
    if (trimmed != NULL) {
        response.data = trimmed;
    }
    *body = response.data;
    *body_length = response.length;
    return ESP_OK;
}

static bool json_uint32(const cJSON *root, const char *name, uint32_t *value)
{
    const cJSON *item = cJSON_GetObjectItemCaseSensitive(root, name);
    if (!cJSON_IsNumber(item) || item->valuedouble < 0 || item->valuedouble > UINT32_MAX) {
        return false;
    }
    *value = (uint32_t)item->valuedouble;
    return item->valuedouble == (double)*value;
}

static bool is_generation(const char *value)
{
    if (value == NULL || strlen(value) != 64) {
        return false;
    }
    for (size_t index = 0; index < 64; ++index) {
        if (!isxdigit((unsigned char)value[index])) {
            return false;
        }
    }
    return true;
}

esp_err_t http_frames_fetch_manifest_after(
    const char *after_generation,
    frame_manifest_t *manifest
)
{
    if (manifest == NULL) {
        return ESP_ERR_INVALID_ARG;
    }
    char path[160] = "/v1/manifest";
    if (after_generation != NULL && after_generation[0] != '\0') {
        if (!is_generation(after_generation)) {
            return ESP_ERR_INVALID_ARG;
        }
        int written = snprintf(
            path,
            sizeof(path),
            "/v1/manifest?after_generation=%s",
            after_generation
        );
        if (written <= 0 || (size_t)written >= sizeof(path)) {
            return ESP_ERR_INVALID_SIZE;
        }
    }
    uint8_t *body = NULL;
    size_t body_length = 0;
    esp_err_t err = http_get_alloc(
        path,
        "application/json",
        8192,
        &body,
        &body_length
    );
    if (err != ESP_OK) {
        return err;
    }

    cJSON *root = cJSON_ParseWithLength((const char *)body, body_length);
    if (root == NULL) {
        heap_caps_free(body);
        return ESP_ERR_INVALID_RESPONSE;
    }
    const cJSON *protocol = cJSON_GetObjectItemCaseSensitive(root, "protocol");
    const cJSON *generation = cJSON_GetObjectItemCaseSensitive(root, "generation");
    const cJSON *format = cJSON_GetObjectItemCaseSensitive(root, "format");
    bool valid = cJSON_IsNumber(protocol) && protocol->valueint == 1
        && cJSON_IsString(generation) && is_generation(generation->valuestring)
        && cJSON_IsString(format) && strcmp(format->valuestring, "jpeg") == 0
        && json_uint32(root, "count", &manifest->count)
        && json_uint32(root, "width", &manifest->width)
        && json_uint32(root, "height", &manifest->height)
        && json_uint32(root, "delay_ms", &manifest->delay_ms);
    if (valid) {
        strlcpy(manifest->generation, generation->valuestring, sizeof(manifest->generation));
    }
    cJSON_Delete(root);
    heap_caps_free(body);
    if (!valid || manifest->count == 0 || manifest->count > 10000
        || manifest->width == 0 || manifest->height == 0
        || manifest->delay_ms < 20 || manifest->delay_ms > 60000) {
        return ESP_ERR_INVALID_RESPONSE;
    }
    ESP_LOGI(
        TAG,
        "manifest: count=%" PRIu32 " size=%" PRIu32 "x%" PRIu32 " generation=%.12s",
        manifest->count,
        manifest->width,
        manifest->height,
        manifest->generation
    );
    return ESP_OK;
}

esp_err_t http_frames_fetch_manifest(frame_manifest_t *manifest)
{
    return http_frames_fetch_manifest_after(NULL, manifest);
}

esp_err_t http_frames_fetch_frame(
    uint32_t index,
    const char *generation,
    uint8_t **jpeg_data,
    size_t *jpeg_size
)
{
    if (generation == NULL || jpeg_data == NULL || jpeg_size == NULL) {
        return ESP_ERR_INVALID_ARG;
    }
    char path[160];
    int written = snprintf(
        path,
        sizeof(path),
        "/v1/frames/%" PRIu32 ".jpg?generation=%s",
        index,
        generation
    );
    if (written <= 0 || (size_t)written >= sizeof(path)) {
        return ESP_ERR_INVALID_SIZE;
    }
    return http_get_alloc(
        path,
        "image/jpeg",
        CONFIG_APP_MAX_JPEG_BYTES,
        jpeg_data,
        jpeg_size
    );
}

void http_frames_free(void *buffer)
{
    heap_caps_free(buffer);
}
