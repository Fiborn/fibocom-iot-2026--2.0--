#include "sc171_udp.h"

#include <errno.h>
#include <stdbool.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include "cloud_player.h"
#include "esp_log.h"
#include "freertos/FreeRTOS.h"
#include "freertos/queue.h"
#include "freertos/task.h"
#include "lwip/inet.h"
#include "lwip/netdb.h"
#include "lwip/sockets.h"
#include "sdkconfig.h"
#include "wifi_manager.h"
#include "ui.h"

/* 监听 SC171 发来的消息（如 NO_SHUTTLE）*/
#define SC171_LISTENER_PORT    9001

static const char *TAG = "sc171_udp";
static QueueHandle_t s_queue;
static volatile bool s_sc171_connected;  // set true on ACK, false on send failure

static bool acknowledgement_matches(const uint8_t *payload, int length, char signal)
{
    return length == 5
        && memcmp(payload, "ACK:", 4) == 0
        && payload[4] == (uint8_t)signal;
}

static esp_err_t send_to_destination(
    int socket_fd,
    const struct sockaddr *destination,
    socklen_t destination_length,
    const char *destination_name,
    char signal
)
{
    for (int attempt = 1; attempt <= CONFIG_APP_SC171_SEND_ATTEMPTS; ++attempt) {
        int sent = sendto(socket_fd, &signal, 1, 0, destination, destination_length);
        if (sent != 1) {
            ESP_LOGW(
                TAG,
                "send '%c' to %s failed on attempt %d/%d (errno=%d)",
                signal,
                destination_name,
                attempt,
                CONFIG_APP_SC171_SEND_ATTEMPTS,
                errno
            );
            continue;
        }

        uint8_t acknowledgement[16];
        struct sockaddr_in sender = {0};
        socklen_t sender_length = sizeof(sender);
        int received = recvfrom(
            socket_fd,
            acknowledgement,
            sizeof(acknowledgement),
            0,
            (struct sockaddr *)&sender,
            &sender_length
        );
        if (received > 0 && acknowledgement_matches(acknowledgement, received, signal)) {
            char sender_ip[INET_ADDRSTRLEN] = {0};
            inet_ntop(AF_INET, &sender.sin_addr, sender_ip, sizeof(sender_ip));
            s_sc171_connected = true;
            ESP_LOGI(
                TAG,
                "SC171 acknowledged signal '%c' from %s:%u",
                signal,
                sender_ip,
                (unsigned)ntohs(sender.sin_port)
            );
            return ESP_OK;
        }
        ESP_LOGW(
            TAG,
            "no valid ACK for '%c' from %s on attempt %d/%d (recv=%d errno=%d)",
            signal,
            destination_name,
            attempt,
            CONFIG_APP_SC171_SEND_ATTEMPTS,
            received,
            received < 0 ? errno : 0
        );
    }
    s_sc171_connected = false;
    return ESP_ERR_TIMEOUT;
}

bool sc171_udp_is_connected(void)
{
    return s_sc171_connected;
}

static esp_err_t send_one(char signal)
{
#if !CONFIG_APP_SC171_ENABLE
    ESP_LOGI(TAG, "SC171 disabled; signal '%c' not sent", signal);
    return ESP_OK;
#else
    esp_err_t err = wifi_manager_wait_connected(30000);
    if (err != ESP_OK) {
        ESP_LOGE(
            TAG,
            "cannot send '%c': P4 did not obtain a Wi-Fi IPv4 address within 30 seconds",
            signal
        );
        return err;
    }

    char port[8];
    snprintf(port, sizeof(port), "%d", CONFIG_APP_SC171_PORT);
    struct addrinfo hints = {
        .ai_family = AF_INET,
        .ai_socktype = SOCK_DGRAM,
    };
    struct addrinfo *addresses = NULL;
    int resolved = getaddrinfo(CONFIG_APP_SC171_HOST, port, &hints, &addresses);
    int socket_fd = socket(AF_INET, SOCK_DGRAM, IPPROTO_UDP);
    if (socket_fd < 0) {
        if (addresses != NULL) {
            freeaddrinfo(addresses);
        }
        return ESP_FAIL;
    }

    struct timeval receive_timeout = {
        .tv_sec = CONFIG_APP_SC171_ACK_TIMEOUT_MS / 1000,
        .tv_usec = (CONFIG_APP_SC171_ACK_TIMEOUT_MS % 1000) * 1000,
    };
    int broadcast_enabled = 1;
    setsockopt(
        socket_fd,
        SOL_SOCKET,
        SO_RCVTIMEO,
        &receive_timeout,
        sizeof(receive_timeout)
    );
    setsockopt(
        socket_fd,
        SOL_SOCKET,
        SO_BROADCAST,
        &broadcast_enabled,
        sizeof(broadcast_enabled)
    );

    err = ESP_ERR_NOT_FOUND;
    if (resolved == 0 && addresses != NULL) {
        ESP_LOGI(
            TAG,
            "sending signal '%c' to configured SC171 %s:%d",
            signal,
            CONFIG_APP_SC171_HOST,
            CONFIG_APP_SC171_PORT
        );
        err = send_to_destination(
            socket_fd,
            addresses->ai_addr,
            addresses->ai_addrlen,
            CONFIG_APP_SC171_HOST,
            signal
        );
    } else {
        ESP_LOGW(TAG, "cannot resolve configured SC171 address %s:%s", CONFIG_APP_SC171_HOST, port);
    }

#if CONFIG_APP_SC171_BROADCAST_FALLBACK
    if (err != ESP_OK) {
        struct sockaddr_in broadcast_address = {
            .sin_family = AF_INET,
            .sin_port = htons(CONFIG_APP_SC171_PORT),
            .sin_addr.s_addr = htonl(INADDR_BROADCAST),
        };
        ESP_LOGW(
            TAG,
            "configured SC171 did not acknowledge '%c'; trying UDP broadcast on port %d",
            signal,
            CONFIG_APP_SC171_PORT
        );
        err = send_to_destination(
            socket_fd,
            (const struct sockaddr *)&broadcast_address,
            sizeof(broadcast_address),
            "255.255.255.255",
            signal
        );
    }
#endif

    close(socket_fd);
    if (addresses != NULL) {
        freeaddrinfo(addresses);
    }
    if (err != ESP_OK) {
        ESP_LOGE(
            TAG,
            "SC171 did not acknowledge '%c'; check its current IPv4 address, UDP port, "
            "firewall, and phone-hotspot client isolation",
            signal
        );
    }
    return err;
#endif
}

static void sender_task(void *argument)
{
    (void)argument;
    char signal;
    while (true) {
        if (xQueueReceive(s_queue, &signal, portMAX_DELAY) == pdTRUE) {
            esp_err_t err = send_one(signal);
            if (err != ESP_OK) {
                ESP_LOGE(TAG, "signal '%c' failed: %s", signal, esp_err_to_name(err));
            }
        }
    }
}

static void listener_task(void *argument)
{
    (void)argument;
    /* 延迟启动，给 wifi_init_task 留时间初始化 lwIP tcpip */
    vTaskDelay(pdMS_TO_TICKS(5000));

    /* 等待 WiFi 连接（此时 tcpip 已就绪，socket() 不会 assert）*/
    wifi_manager_wait_connected(portMAX_DELAY);

    int sock = socket(AF_INET, SOCK_DGRAM, IPPROTO_UDP);
    if (sock < 0) {
        ESP_LOGE(TAG, "listener: cannot create socket");
        vTaskDelete(NULL);
        return;
    }

    struct sockaddr_in addr = {
        .sin_family = AF_INET,
        .sin_port = htons(SC171_LISTENER_PORT),
        .sin_addr.s_addr = htonl(INADDR_ANY),
    };
    if (bind(sock, (struct sockaddr *)&addr, sizeof(addr)) != 0) {
        ESP_LOGE(TAG, "listener: bind port %d failed (errno=%d)", SC171_LISTENER_PORT, errno);
        close(sock);
        vTaskDelete(NULL);
        return;
    }

    ESP_LOGI(TAG, "listener ready on UDP %d for incoming SC171 messages", SC171_LISTENER_PORT);

    uint8_t buf[32];
    struct sockaddr_in sender;
    socklen_t sender_len = sizeof(sender);

    while (true) {
        int n = recvfrom(sock, buf, sizeof(buf) - 1, 0,
                         (struct sockaddr *)&sender, &sender_len);
        if (n <= 0) {
            continue;
        }
        buf[n] = '\0';

        /* 去除末尾换行 */
        while (n > 0 && (buf[n - 1] == '\n' || buf[n - 1] == '\r')) {
            buf[--n] = '\0';
        }

        if (strcmp((const char *)buf, "NO_SHUTTLE") == 0) {
            char ip[INET_ADDRSTRLEN] = {0};
            inet_ntop(AF_INET, &sender.sin_addr, ip, sizeof(ip));
            ESP_LOGI(TAG, "received NO_SHUTTLE from %s:%u — returning to home",
                     ip, (unsigned)ntohs(sender.sin_port));
            ui_set_return_requested();
        } else if (strncmp((const char *)buf, "PROGRESS:SC171:", 15) == 0) {
            int percent = atoi((const char *)buf + 15);
            if (percent >= 0 && percent <= 100) {
                ui_set_sc171_progress(percent);
            }
        } else if (strcmp((const char *)buf, "FRAMES_READY") == 0) {
            char ip[INET_ADDRSTRLEN] = {0};
            inet_ntop(AF_INET, &sender.sin_addr, ip, sizeof(ip));
            ESP_LOGI(TAG, "received FRAMES_READY from %s — waking cloud player", ip);
            cloud_player_request_poll();
        } else {
            ESP_LOGD(TAG, "ignored unknown message from SC171: %s", (const char *)buf);
        }
    }

    close(sock);
    vTaskDelete(NULL);
}

esp_err_t sc171_udp_init(void)
{
    if (s_queue != NULL) {
        return ESP_OK;
    }
    s_queue = xQueueCreate(8, sizeof(char));
    if (s_queue == NULL) {
        return ESP_ERR_NO_MEM;
    }
    BaseType_t created = xTaskCreate(sender_task, "sc171_udp", 4096, NULL, 6, NULL);
    if (created != pdPASS) {
        vQueueDelete(s_queue);
        s_queue = NULL;
        return ESP_ERR_NO_MEM;
    }

    /* 启动监听器，接收 SC171 发来的 NO_SHUTTLE 等消息 */
    created = xTaskCreate(listener_task, "sc171_lsn", 3072, NULL, 5, NULL);
    if (created != pdPASS) {
        ESP_LOGW(TAG, "listener task creation failed (non-fatal)");
    }
    ESP_LOGI(
        TAG,
        "sender ready for %s:%d; commands wait for Wi-Fi when necessary",
        CONFIG_APP_SC171_HOST,
        CONFIG_APP_SC171_PORT
    );
    return ESP_OK;
}

esp_err_t sc171_udp_send(char signal)
{
    if (s_queue == NULL || (signal != 'a' && signal != 'b' && signal != 'c')) {
        return ESP_ERR_INVALID_ARG;
    }
    if (xQueueSend(s_queue, &signal, 0) != pdTRUE) {
        ESP_LOGE(TAG, "send queue is full; signal '%c' dropped", signal);
        return ESP_ERR_TIMEOUT;
    }
    ESP_LOGI(TAG, "queued signal '%c'", signal);
    return ESP_OK;
}
