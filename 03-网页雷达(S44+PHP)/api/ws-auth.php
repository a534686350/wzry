<?php
declare(strict_types=1);

/**
 * 仅供 Nginx auth_request 内部子请求调用。
 * 浏览器在登录或验证成功后会获得 HttpOnly 的 radar_ws_token Cookie，
 * Nginx 在把 /ws 转发到 Java 服务前使用本接口校验该会话。
 */
require_once __DIR__ . '/db.php';

try {
    $token = getSessionTokenFromCookie();
    if (!$token || !validateSession(getDb(), $token)) {
        http_response_code(401);
        exit;
    }

    http_response_code(204);
    exit;
} catch (Throwable $e) {
    error_log('Radar WebSocket auth error: ' . $e->getMessage());
    http_response_code(500);
    exit;
}
