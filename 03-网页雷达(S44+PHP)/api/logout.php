<?php
declare(strict_types=1);

require_once __DIR__ . '/db.php';

try {
    setCorsHeaders();

    if (($_SERVER['REQUEST_METHOD'] ?? 'GET') !== 'POST') {
        jsonResponse(['success' => false, 'message' => '请使用 POST 请求'], 405);
    }

    $token = getBearerToken();
    if (!$token) {
        jsonResponse(['success' => false, 'message' => '未提供有效登录令牌'], 401);
    }

    $db = getDb();
    $stmt = $db->prepare('DELETE FROM sessions WHERE token = ?');
    $stmt->execute([$token]);
    clearAuthCookie();

    jsonResponse(['success' => true, 'message' => '已退出登录']);
} catch (Throwable $e) {
    error_log('Radar auth logout error: ' . $e->getMessage());
    jsonResponse(['success' => false, 'message' => '服务器错误，请稍后重试'], 500);
}

