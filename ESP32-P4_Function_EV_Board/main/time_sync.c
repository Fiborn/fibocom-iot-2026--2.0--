#include "time_sync.h"

#include <string.h>
#include <time.h>

#include "esp_check.h"
#include "esp_log.h"
#include "esp_netif_sntp.h"
#include "freertos/FreeRTOS.h"
#include "sdkconfig.h"

static const char *TAG = "time_sync";

esp_err_t time_sync_for_tls(void)
{
    if (strncmp(CONFIG_APP_SERVER_BASE_URL, "https://", 8) != 0) {
        ESP_LOGW(TAG, "plain HTTP configured; skipping clock synchronization");
        return ESP_OK;
    }
    time_t now = time(NULL);
    if (now >= 1735689600) {
        return ESP_OK;
    }
    esp_sntp_config_t config = ESP_NETIF_SNTP_DEFAULT_CONFIG(CONFIG_APP_SNTP_SERVER);
    ESP_RETURN_ON_ERROR(esp_netif_sntp_init(&config), TAG, "SNTP init failed");
    esp_err_t err = esp_netif_sntp_sync_wait(pdMS_TO_TICKS(CONFIG_APP_SNTP_TIMEOUT_MS));
    esp_netif_sntp_deinit();
    if (err != ESP_OK) {
        return err;
    }
    now = time(NULL);
    struct tm utc = {0};
    char timestamp[32];
    gmtime_r(&now, &utc);
    strftime(timestamp, sizeof(timestamp), "%Y-%m-%dT%H:%M:%SZ", &utc);
    ESP_LOGI(TAG, "clock synchronized: %s", timestamp);
    return ESP_OK;
}
