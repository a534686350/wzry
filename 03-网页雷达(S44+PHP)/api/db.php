<?php
declare(strict_types=1);

function getConfig(): array
{
    static $config = null;

    if ($config !== null) {
        return $config;
    }

    $path = __DIR__ . '/config.php';
    if (!is_file($path)) {
        throw new RuntimeException('配置文件不存在，请先访问 /install.php 完成安装。');
    }

    $config = require $path;
    if (!is_array($config) || empty($config['db']) || !is_array($config['db'])) {
        throw new RuntimeException('配置文件格式无效。');
    }

    return $config;
}

function getDb(): PDO
{
    static $pdo = null;

    if ($pdo instanceof PDO) {
        return $pdo;
    }

    $cfg = getConfig()['db'];
    $host = (string)($cfg['host'] ?? '127.0.0.1');
    $port = (int)($cfg['port'] ?? 3306);
    $database = (string)($cfg['database'] ?? '');
    $charset = (string)($cfg['charset'] ?? 'utf8mb4');

    if ($database === '') {
        throw new RuntimeException('数据库名称未配置。');
    }

    $dsn = sprintf(
        'mysql:host=%s;port=%d;dbname=%s;charset=%s',
        $host,
        $port,
        $database,
        $charset
    );

    $pdo = new PDO($dsn, (string)($cfg['username'] ?? ''), (string)($cfg['password'] ?? ''), [
        PDO::ATTR_ERRMODE            => PDO::ERRMODE_EXCEPTION,
        PDO::ATTR_DEFAULT_FETCH_MODE => PDO::FETCH_ASSOC,
        PDO::ATTR_EMULATE_PREPARES   => false,
    ]);

    return $pdo;
}

function jsonResponse(array $data, int $code = 200): void
{
    http_response_code($code);
    header('Content-Type: application/json; charset=utf-8');
    echo json_encode($data, JSON_UNESCAPED_UNICODE | JSON_INVALID_UTF8_SUBSTITUTE);
    exit;
}

function setCorsHeaders(): void
{
    $configuredOrigins = getConfig()['cors_origin'] ?? '';
    $requestOrigin = trim((string)($_SERVER['HTTP_ORIGIN'] ?? ''));

    if (is_string($configuredOrigins)) {
        $configuredOrigins = trim($configuredOrigins);
        $origins = $configuredOrigins === '' ? [] : [$configuredOrigins];
    } elseif (is_array($configuredOrigins)) {
        $origins = array_values(array_filter(array_map('strval', $configuredOrigins)));
    } else {
        $origins = [];
    }

    if (in_array('*', $origins, true)) {
        header('Access-Control-Allow-Origin: *');
    } elseif ($requestOrigin !== '' && in_array($requestOrigin, $origins, true)) {
        header('Access-Control-Allow-Origin: ' . $requestOrigin);
        header('Vary: Origin');
    }

    header('Access-Control-Allow-Methods: GET, POST, OPTIONS');
    header('Access-Control-Allow-Headers: Content-Type, Authorization');
    header('Access-Control-Max-Age: 86400');

    if (($_SERVER['REQUEST_METHOD'] ?? 'GET') === 'OPTIONS') {
        http_response_code(204);
        exit;
    }
}

function getClientIp(): string
{
    $remoteAddr = trim((string)($_SERVER['REMOTE_ADDR'] ?? ''));
    if (!filter_var($remoteAddr, FILTER_VALIDATE_IP)) {
        return '0.0.0.0';
    }

    $trustedProxies = getConfig()['trusted_proxies'] ?? [];
    if (!is_array($trustedProxies) || !in_array($remoteAddr, $trustedProxies, true)) {
        return $remoteAddr;
    }

    foreach (['HTTP_X_FORWARDED_FOR', 'HTTP_X_REAL_IP'] as $header) {
        $value = (string)($_SERVER[$header] ?? '');
        foreach (explode(',', $value) as $candidate) {
            $ip = trim($candidate);
            if (filter_var($ip, FILTER_VALIDATE_IP)) {
                return $ip;
            }
        }
    }

    return $remoteAddr;
}

function isHttpsRequest(): bool
{
    return (!empty($_SERVER['HTTPS']) && $_SERVER['HTTPS'] !== 'off')
        || (int)($_SERVER['SERVER_PORT'] ?? 80) === 443;
}

function setAuthCookie(string $token, int $expiresAt): void
{
    setcookie('radar_ws_token', $token, [
        'expires' => $expiresAt,
        'path' => '/',
        'secure' => isHttpsRequest(),
        'httponly' => true,
        'samesite' => 'Strict',
    ]);
}

function clearAuthCookie(): void
{
    setcookie('radar_ws_token', '', [
        'expires' => time() - 3600,
        'path' => '/',
        'secure' => isHttpsRequest(),
        'httponly' => true,
        'samesite' => 'Strict',
    ]);
}

function getSessionTokenFromCookie(): ?string
{
    $token = trim((string)($_COOKIE['radar_ws_token'] ?? ''));
    return preg_match('/^[a-f0-9]{64}$/i', $token) ? strtolower($token) : null;
}

function getBearerToken(): ?string
{
    $auth = (string)($_SERVER['HTTP_AUTHORIZATION'] ?? $_SERVER['REDIRECT_HTTP_AUTHORIZATION'] ?? '');
    if (preg_match('/Bearer\\s+([a-f0-9]{64})$/i', trim($auth), $matches)) {
        return strtolower($matches[1]);
    }

    if (!empty($_POST['token']) && preg_match('/^[a-f0-9]{64}$/i', trim((string)$_POST['token']))) {
        return strtolower(trim((string)$_POST['token']));
    }

    $rawInput = file_get_contents('php://input');
    $input = is_string($rawInput) ? json_decode($rawInput, true) : null;
    if (is_array($input) && !empty($input['token']) && preg_match('/^[a-f0-9]{64}$/i', trim((string)$input['token']))) {
        return strtolower(trim((string)$input['token']));
    }

    return null;
}

function generateToken(): string
{
    return bin2hex(random_bytes(32));
}

function generateCardKey(): string
{
    $chars = 'ABCDEFGHJKLMNPQRSTUVWXYZ23456789';
    $segments = [];

    for ($segmentIndex = 0; $segmentIndex < 4; $segmentIndex++) {
        $segment = '';
        for ($charIndex = 0; $charIndex < 4; $charIndex++) {
            $segment .= $chars[random_int(0, strlen($chars) - 1)];
        }
        $segments[] = $segment;
    }

    return implode('-', $segments);
}

function expireStaleKeys(PDO $db): void
{
    $db->exec("UPDATE card_keys SET status = 'expired' WHERE status = 'active' AND expires_at IS NOT NULL AND expires_at < NOW()");
}

function purgeExpiredSessions(PDO $db): void
{
    $db->exec('DELETE FROM sessions WHERE expires_at <= NOW()');
}

function validateSession(PDO $db, string $token): ?array
{
    expireStaleKeys($db);
    purgeExpiredSessions($db);

    $stmt = $db->prepare(
        'SELECT s.*, ck.card_key, ck.status AS key_status, ck.expires_at AS key_expires_at,
                ck.duration_days, ck.bind_ip AS key_bind_ip
         FROM sessions s
         JOIN card_keys ck ON ck.id = s.card_key_id
         WHERE s.token = ? AND s.expires_at > NOW()'
    );
    $stmt->execute([$token]);
    $session = $stmt->fetch();

    if (!$session || $session['key_status'] === 'banned') {
        return null;
    }

    if ($session['key_status'] === 'expired' || ($session['key_expires_at'] && strtotime($session['key_expires_at']) < time())) {
        $db->prepare("UPDATE card_keys SET status = 'expired' WHERE id = ?")->execute([$session['card_key_id']]);
        return null;
    }

    $config = getConfig();
    if (
        !empty($config['bind_ip']) &&
        !empty($session['key_bind_ip']) &&
        !hash_equals((string)$session['key_bind_ip'], getClientIp())
    ) {
        return null;
    }

    $db->prepare('UPDATE sessions SET last_active = NOW() WHERE id = ?')->execute([$session['id']]);

    return $session;
}

