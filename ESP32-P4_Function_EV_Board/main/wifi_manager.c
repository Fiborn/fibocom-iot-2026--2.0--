#include "wifi_manager.h"

#include <string.h>

#include "esp_check.h"
#include "esp_event.h"
#include "esp_log.h"
#include "esp_netif.h"
#include "esp_wifi.h"
#include "freertos/FreeRTOS.h"
#include "freertos/event_groups.h"
#include "freertos/task.h"
#include "sdkconfig.h"

static const char *TAG = "wifi_manager";
static const EventBits_t WIFI_CONNECTED_BIT = BIT0;
static EventGroupHandle_t s_events;
static esp_netif_t *s_station_netif;
static bool s_platform_ready;
static bool s_wifi_ready;
static bool s_wifi_handler_ready;
static bool s_ip_handler_ready;
static bool s_station_started;
static unsigned s_disconnect_count;

static void wifi_event_handler(void *arg, esp_event_base_t event_base, int32_t event_id, void *event_data)
{
    (void)arg;
    if (event_base == WIFI_EVENT && event_id == WIFI_EVENT_STA_START) {
        esp_err_t err = esp_wifi_connect();
        if (err != ESP_OK) {
            ESP_LOGW(TAG, "initial Wi-Fi connect request failed: %s", esp_err_to_name(err));
        }
    } else if (event_base == WIFI_EVENT && event_id == WIFI_EVENT_STA_DISCONNECTED) {
        const wifi_event_sta_disconnected_t *disconnected =
            (const wifi_event_sta_disconnected_t *)event_data;
        xEventGroupClearBits(s_events, WIFI_CONNECTED_BIT);
        ++s_disconnect_count;
        ESP_LOGW(
            TAG,
            "Wi-Fi disconnected (reason=%u, count=%u); reconnecting",
            disconnected != NULL ? disconnected->reason : 0,
            s_disconnect_count
        );
        esp_err_t err = esp_wifi_connect();
        if (err != ESP_OK) {
            ESP_LOGW(TAG, "automatic Wi-Fi reconnect request failed: %s", esp_err_to_name(err));
        }
    } else if (event_base == IP_EVENT && event_id == IP_EVENT_STA_GOT_IP) {
        const ip_event_got_ip_t *got_ip = (const ip_event_got_ip_t *)event_data;
        s_disconnect_count = 0;
        ESP_LOGI(TAG, "connected, IPv4=" IPSTR, IP2STR(&got_ip->ip_info.ip));
        xEventGroupSetBits(s_events, WIFI_CONNECTED_BIT);
    }
}

static esp_err_t initialize_network_platform(void)
{
    if (s_events == NULL) {
        s_events = xEventGroupCreate();
        if (s_events == NULL) {
            return ESP_ERR_NO_MEM;
        }
    }

    if (!s_platform_ready) {
        esp_err_t err = esp_netif_init();
        if (err != ESP_OK && err != ESP_ERR_INVALID_STATE) {
            ESP_LOGE(TAG, "esp_netif_init failed: %s", esp_err_to_name(err));
            return err;
        }
        err = esp_event_loop_create_default();
        if (err != ESP_OK && err != ESP_ERR_INVALID_STATE) {
            ESP_LOGE(TAG, "event loop creation failed: %s", esp_err_to_name(err));
            return err;
        }
        s_platform_ready = true;
    }

    if (s_station_netif == NULL) {
        s_station_netif = esp_netif_create_default_wifi_sta();
        if (s_station_netif == NULL) {
            return ESP_FAIL;
        }
    }
    return ESP_OK;
}

esp_err_t wifi_manager_init(void)
{
    ESP_LOGI(TAG, "starting station for SSID '%s'", CONFIG_APP_WIFI_SSID);
    ESP_RETURN_ON_ERROR(
        initialize_network_platform(),
        TAG,
        "network platform initialization failed"
    );

    if (!s_wifi_ready) {
        wifi_init_config_t init_config = WIFI_INIT_CONFIG_DEFAULT();
        esp_err_t err = esp_wifi_init(&init_config);
        if (err != ESP_OK && err != ESP_ERR_WIFI_INIT_STATE) {
            ESP_LOGE(TAG, "esp_wifi_init failed: %s", esp_err_to_name(err));
            return err;
        }
        s_wifi_ready = true;
    }

    if (!s_wifi_handler_ready) {
        ESP_RETURN_ON_ERROR(
            esp_event_handler_register(WIFI_EVENT, ESP_EVENT_ANY_ID, &wifi_event_handler, NULL),
            TAG,
            "register Wi-Fi handler failed"
        );
        s_wifi_handler_ready = true;
    }
    if (!s_ip_handler_ready) {
        ESP_RETURN_ON_ERROR(
            esp_event_handler_register(IP_EVENT, IP_EVENT_STA_GOT_IP, &wifi_event_handler, NULL),
            TAG,
            "register IP handler failed"
        );
        s_ip_handler_ready = true;
    }

    wifi_config_t wifi_config = {0};
    strlcpy((char *)wifi_config.sta.ssid, CONFIG_APP_WIFI_SSID, sizeof(wifi_config.sta.ssid));
    strlcpy(
        (char *)wifi_config.sta.password,
        CONFIG_APP_WIFI_PASSWORD,
        sizeof(wifi_config.sta.password)
    );
    wifi_config.sta.threshold.authmode = WIFI_AUTH_OPEN;
    wifi_config.sta.pmf_cfg.capable = true;
    wifi_config.sta.pmf_cfg.required = false;

    ESP_RETURN_ON_ERROR(esp_wifi_set_mode(WIFI_MODE_STA), TAG, "set station mode failed");
    ESP_RETURN_ON_ERROR(esp_wifi_set_config(WIFI_IF_STA, &wifi_config), TAG, "set Wi-Fi config failed");
    if (!s_station_started) {
        ESP_RETURN_ON_ERROR(esp_wifi_start(), TAG, "esp_wifi_start failed");
        s_station_started = true;
    } else {
        esp_err_t connect_err = esp_wifi_connect();
        if (connect_err != ESP_OK) {
            ESP_LOGW(
                TAG,
                "station already started, connect request failed: %s",
                esp_err_to_name(connect_err)
            );
        }
    }
    ESP_ERROR_CHECK_WITHOUT_ABORT(esp_wifi_set_ps(WIFI_PS_NONE));
    ESP_LOGI(TAG, "station started; association is asynchronous");
    return ESP_OK;
}

esp_err_t wifi_manager_wait_connected(uint32_t timeout_ms)
{
    if (s_events == NULL) {
        return ESP_ERR_INVALID_STATE;
    }
    TickType_t ticks = timeout_ms == UINT32_MAX ? portMAX_DELAY : pdMS_TO_TICKS(timeout_ms);
    EventBits_t bits = xEventGroupWaitBits(s_events, WIFI_CONNECTED_BIT, pdFALSE, pdTRUE, ticks);
    if ((bits & WIFI_CONNECTED_BIT) != 0) {
        return ESP_OK;
    }

    ESP_LOGW(TAG, "Wi-Fi wait timed out after %u ms; requesting reconnect", (unsigned)timeout_ms);
    if (s_wifi_ready && s_station_started) {
        esp_err_t err = esp_wifi_connect();
        if (err != ESP_OK) {
            ESP_LOGW(TAG, "connect request after timeout failed: %s", esp_err_to_name(err));
        }
    }
    return ESP_ERR_TIMEOUT;
}

esp_err_t wifi_manager_reconnect(void)
{
    if (s_events == NULL || !s_wifi_ready || !s_station_started) {
        return ESP_ERR_INVALID_STATE;
    }

    xEventGroupClearBits(s_events, WIFI_CONNECTED_BIT);
    ESP_LOGW(TAG, "forcing Wi-Fi station reconnect");

    esp_err_t disconnect_err = esp_wifi_disconnect();
    if (disconnect_err != ESP_OK && disconnect_err != ESP_ERR_WIFI_NOT_CONNECT) {
        ESP_LOGW(TAG, "esp_wifi_disconnect failed: %s", esp_err_to_name(disconnect_err));
    }
    vTaskDelay(pdMS_TO_TICKS(250));

    esp_err_t err = esp_wifi_connect();
    if (err == ESP_OK || err == ESP_ERR_WIFI_CONN) {
        return ESP_OK;
    }

    ESP_LOGW(TAG, "direct reconnect failed: %s; restarting station", esp_err_to_name(err));
    err = esp_wifi_stop();
    if (err != ESP_OK) {
        ESP_LOGE(TAG, "esp_wifi_stop failed during recovery: %s", esp_err_to_name(err));
        return err;
    }
    s_station_started = false;
    vTaskDelay(pdMS_TO_TICKS(500));

    err = esp_wifi_start();
    if (err == ESP_OK) {
        s_station_started = true;
    }
    return err;
}

bool wifi_manager_is_connected(void)
{
    if (s_events == NULL) {
        return false;
    }
    return (xEventGroupGetBits(s_events) & WIFI_CONNECTED_BIT) != 0;
}
