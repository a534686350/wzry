<?php
declare(strict_types=1);

require_once __DIR__ . '/db.php';

try {
    setCorsHeaders();

    if (($_SERVER['REQUEST_METHOD'] ?? 'GET') !== 'POST') {
        jsonResponse(['success' => false, 'message' => '请使用 POST 请求'], 405);
    }

    $rawInput = file_get_contents('php://input');
    $input = is_string($rawInput) ? json_decode($rawInput, true) : null;
    if (!is_array($input)) {
        $input = $_POST;
    }

    $cardKey = strtoupper(trim((string)($input['card_key'] ?? '')));
    if (!preg_match('/^[A-Z0-9]{4}(?:-[A-Z0-9]{4}){3}$/', $cardKey)) {
        jsonResponse(['success' => false, 'message' => '卡密格式不正确'], 400);
    }

    $config = getConfig();
    $db = getDb();
    $db->beginTransaction();

    $stmt = $db->prepare('SELECT * FROM card_keys WHERE card_key = ? FOR UPDATE');
    $stmt->execute([$cardKey]);
    $key = $stmt->fetch();

    if (!$key) {
        $db->rollBack();
        jsonResponse(['success' => false, 'message' => '卡密不存在'], 401);
    }

    if ($key['status'] === 'banned') {
        $db->rollBack();
        jsonResponse(['success' => false, 'message' => '卡密已被封禁'], 403);
    }

    if ($key['status'] === 'expired' || ($key['expires_at'] && strtotime($key['expires_at']) < time())) {
        $db->prepare("UPDATE card_keys SET status = 'expired' WHERE id = ?")->execute([$key['id']]);
        $db->commit();
        jsonResponse(['success' => false, 'message' => '卡密已过期'], 401);
    }

    $clientIp = getClientIp();

    if ($key['status'] === 'unused') {
        $durationDays = max(1, min(36500, (int)$key['duration_days']));
        $update = $db->prepare(
            "UPDATE card_keys
             SET status = 'active', activated_at = NOW(),
                 expires_at = DATE_ADD(NOW(), INTERVAL {$durationDays} DAY),
                 bind_ip = ?
             WHERE id = ?"
        );
        $update->execute([!empty($config['bind_ip']) ? $clientIp : null, $key['id']]);

        $refresh = $db->prepare('SELECT status, expires_at, bind_ip FROM card_keys WHERE id = ?');
        $refresh->execute([$key['id']]);
        $updatedKey = $refresh->fetch();
        if (!$updatedKey) {
            throw new RuntimeException('卡密激活状态读取失败。');
        }
        $key = array_merge($key, $updatedKey);
    } elseif (!empty($config['bind_ip']) && !empty($key['bind_ip']) && !hash_equals((string)$key['bind_ip'], $clientIp)) {
        $db->rollBack();
        jsonResponse(['success' => false, 'message' => '该卡密已绑定其他 IP，无法在此设备登录'], 403);
    }

    purgeExpiredSessions($db);
    $maxSessions = max(0, (int)($config['max_sessions_per_key'] ?? 1));
    if ($maxSessions > 0) {
        $countStmt = $db->prepare('SELECT COUNT(*) FROM sessions WHERE card_key_id = ? AND expires_at > NOW()');
        $countStmt->execute([$key['id']]);
        $activeCount = (int)$countStmt->fetchColumn();
        $deleteCount = max(0, $activeCount - $maxSessions + 1);

        if ($deleteCount > 0) {
            $deleteOldest = $db->prepare(
                'DELETE FROM sessions
                 WHERE card_key_id = ? AND expires_at > NOW()
                 ORDER BY last_active ASC, id ASC
                 LIMIT ' . $deleteCount
            );
            $deleteOldest->execute([$key['id']]);
        }
    }

    $token = generateToken();
    $insertSession = $db->prepare(
        'INSERT INTO sessions (card_key_id, token, ip_address, user_agent, expires_at) VALUES (?, ?, ?, ?, ?)'
    );
    $insertSession->execute([
        $key['id'],
        $token,
        $clientIp,
        substr((string)($_SERVER['HTTP_USER_AGENT'] ?? ''), 0, 512),
        $key['expires_at'],
    ]);

    $db->commit();

    $expiresTimestamp = strtotime((string)$key['expires_at']);
    setAuthCookie($token, $expiresTimestamp === false ? time() : $expiresTimestamp);
    $remainingSeconds = max(0, $expiresTimestamp === false ? 0 : $expiresTimestamp - time());
    jsonResponse([
        'success' => true,
        'message' => '登录成功',
        'token' => $token,
        'expires_at' => $key['expires_at'],
        'remaining_seconds' => $remainingSeconds,
        'remaining_days' => (int)ceil($remainingSeconds / 86400),
    ]);
} catch (Throwable $e) {
    if (isset($db) && $db instanceof PDO && $db->inTransaction()) {
        $db->rollBack();
    }

    error_log('Radar auth login error: ' . $e->getMessage());
    jsonResponse(['success' => false, 'message' => '服务器错误，请稍后重试'], 500);
}

