#pragma once

#include <stddef.h>
#include <stdint.h>

#include "esp_err.h"

typedef struct {
    char generation[65];
    uint32_t count;
    uint32_t width;
    uint32_t height;
    uint32_t delay_ms;
} frame_manifest_t;

esp_err_t http_frames_fetch_manifest(frame_manifest_t *manifest);
esp_err_t http_frames_fetch_manifest_after(
    const char *after_generation,
    frame_manifest_t *manifest
);
esp_err_t http_frames_fetch_frame(
    uint32_t index,
    const char *generation,
    uint8_t **jpeg_data,
    size_t *jpeg_size
);
void http_frames_free(void *buffer);
