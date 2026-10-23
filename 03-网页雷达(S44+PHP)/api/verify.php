<?php
declare(strict_types=1);

require_once __DIR__ . '/db.php';

try {
    setCorsHeaders();

    $token = getBearerToken();
    if (!$token) {
        jsonResponse(['success' => false, 'message' => '未提供有效登录令牌', 'authenticated' => false], 401);
    }

    $session = validateSession(getDb(), $token);
    if (!$session) {
        jsonResponse(['success' => false, 'message' => '登录已失效，请重新输入卡密', 'authenticated' => false], 401);
    }

    $expiresTimestamp = strtotime((string)$session['key_expires_at']);
    setAuthCookie($token, $expiresTimestamp === false ? time() : $expiresTimestamp);
    $remainingSeconds = max(0, $expiresTimestamp === false ? 0 : $expiresTimestamp - time());
    jsonResponse([
        'success' => true,
        'authenticated' => true,
        'expires_at' => $session['key_expires_at'],
        'remaining_seconds' => $remainingSeconds,
        'remaining_days' => (int)ceil($remainingSeconds / 86400),
        'card_key_masked' => substr((string)$session['card_key'], 0, 4) . '****' . substr((string)$session['card_key'], -4),
    ]);
} catch (Throwable $e) {
    error_log('Radar auth verify error: ' . $e->getMessage());
    jsonResponse(['success' => false, 'message' => '服务器错误，请稍后重试', 'authenticated' => false], 500);
}

