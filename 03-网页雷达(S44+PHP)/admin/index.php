<?php
declare(strict_types=1);

require_once __DIR__ . '/../api/db.php';

$https = (!empty($_SERVER['HTTPS']) && $_SERVER['HTTPS'] !== 'off')
    || (int)($_SERVER['SERVER_PORT'] ?? 80) === 443;
session_name('radar_admin');
session_set_cookie_params([
    'lifetime' => 0,
    'path' => '/',
    'secure' => $https,
    'httponly' => true,
    'samesite' => 'Strict',
]);
session_start();

function h(string $value): string
{
    return htmlspecialchars($value, ENT_QUOTES | ENT_SUBSTITUTE, 'UTF-8');
}

function csrfToken(): string
{
    if (empty($_SESSION['csrf_token'])) {
        $_SESSION['csrf_token'] = bin2hex(random_bytes(32));
    }

    return (string)$_SESSION['csrf_token'];
}

function isValidCsrfToken(?string $token): bool
{
    return is_string($token)
        && !empty($_SESSION['csrf_token'])
        && hash_equals((string)$_SESSION['csrf_token'], $token);
}

function setFlash(string $type, string $message): void
{
    $_SESSION['flash'] = ['type' => $type, 'message' => $message];
}

function pullFlash(): ?array
{
    $flash = $_SESSION['flash'] ?? null;
    unset($_SESSION['flash']);

    return is_array($flash) ? $flash : null;
}

function verifyAdminPassword(array $config, string $password): bool
{
    $hash = trim((string)($config['admin_password_hash'] ?? ''));
    if ($hash !== '') {
        return password_verify($password, $hash);
    }

    // 兼容旧版明文配置；新安装由 install.php 写入哈希，不会走该分支。
    $legacyPassword = (string)($config['admin_password'] ?? '');
    return $legacyPassword !== '' && hash_equals($legacyPassword, $password);
}

function verifySuperPassword(array $config, string $password): bool
{
    $hash = trim((string)($config['super_password_hash'] ?? ''));
    return $hash !== '' && password_verify($password, $hash);
}

function writeAdminConfig(array $config): void
{
    $path = __DIR__ . '/../api/config.php';
    $temporaryPath = $path . '.tmp';
    $contents = "<?php\ndeclare(strict_types=1);\n\nreturn " . var_export($config, true) . ";\n";

    if (file_put_contents($temporaryPath, $contents, LOCK_EX) === false || !@rename($temporaryPath, $path)) {
        @unlink($temporaryPath);
        throw new RuntimeException('无法写入配置文件，请检查 api 目录权限。');
    }

    @chmod($path, 0640);
}

function redirectSelf(string $target = 'index.php'): void
{
    header('Location: ' . $target, true, 303);
    exit;
}

function renderHeader(string $title = '卡密管理'): void
{
    echo '<!DOCTYPE html><html lang="zh-CN"><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1">';
    echo '<title>' . h($title) . '</title>';
    echo '<style>
        *{box-sizing:border-box;margin:0;padding:0}
        body{font-family:-apple-system,BlinkMacSystemFont,"Segoe UI","Microsoft YaHei",sans-serif;background:#0f1419;color:#e0e0e0;padding:20px;line-height:1.6}
        .wrap{max-width:1150px;margin:0 auto}.top{display:flex;align-items:center;justify-content:space-between;gap:12px;margin-bottom:20px}
        h1{color:#4a9eff;font-size:26px}.card{background:#1a1f24;border:1px solid #2a3440;border-radius:12px;padding:20px;margin-bottom:20px}
        label{display:block;margin-bottom:6px;color:#aaa;font-size:14px}input,textarea{width:100%;padding:10px 12px;border:1px solid #333;border-radius:8px;background:#0a0e12;color:#fff;margin-bottom:12px}
        button,.btn{display:inline-block;padding:10px 18px;border:0;border-radius:8px;background:#4a9eff;color:#fff;cursor:pointer;text-decoration:none;font-size:14px}.btn:hover,button:hover{background:#3a8eef}
        .btn-danger{background:#e74c3c}.btn-danger:hover{background:#c0392b}.btn-sm{padding:6px 12px;font-size:12px}.btn-muted{background:#58616a}.btn-muted:hover{background:#454d54}
        table{width:100%;border-collapse:collapse;margin-top:10px;font-size:13px}th,td{padding:10px;border-bottom:1px solid #2a3440;text-align:left;vertical-align:top}th{color:#4a9eff}
        .status-unused{color:#f1c40f}.status-active{color:#2ecc71}.status-expired{color:#95a5a6}.status-banned{color:#e74c3c}.msg{padding:12px;border-radius:8px;margin-bottom:15px}.msg-ok{background:rgba(46,204,113,.15);border:1px solid #2ecc71}.msg-err{background:rgba(231,76,60,.15);border:1px solid #e74c3c}
        .flex{display:flex;gap:10px;flex-wrap:wrap;align-items:end}.flex>*{flex:1;min-width:120px}.inline{display:inline}.inline+ .inline{margin-left:4px}code{background:#0a0e12;padding:2px 6px;border-radius:4px;color:#4a9eff}.muted{color:#999;font-size:13px}a{color:#69b2ff;text-decoration:none}a:hover{text-decoration:underline}
        @media(max-width:760px){body{padding:12px}.card{padding:15px}.table-wrap{overflow:auto}table{min-width:900px}}
    </style></head><body><div class="wrap">';
}

function renderFooter(): void
{
    echo '</div></body></html>';
}

try {
    $config = getConfig();
    $db = getDb();
} catch (Throwable $e) {
    http_response_code(500);
    renderHeader('管理后台配置错误');
    echo '<div class="card"><h2>管理后台暂不可用</h2><p class="muted">请先访问 <code>/install.php</code> 完成安装，并检查数据库连接配置。</p></div>';
    renderFooter();
    exit;
}

// 重设后台密码后，配置中的版本会更新；旧浏览器会话随即失效。
$adminSessionVersion = trim((string)($config['admin_session_version'] ?? ''));
if (!empty($_SESSION['admin_logged_in']) && $adminSessionVersion !== '') {
    $sessionVersion = (string)($_SESSION['admin_session_version'] ?? '');
    if ($sessionVersion === '' || !hash_equals($adminSessionVersion, $sessionVersion)) {
        $_SESSION = [];
        session_destroy();
        redirectSelf();
    }
}

$method = $_SERVER['REQUEST_METHOD'] ?? 'GET';
$action = (string)($_POST['action'] ?? '');
$showForgotPasswordForm = isset($_GET['forgot']) && $_GET['forgot'] === '1';

if ($method === 'POST' && $action === 'reset_password') {
    $resetError = null;
    if (!isValidCsrfToken($_POST['csrf_token'] ?? null)) {
        $resetError = '页面已失效，请刷新后重试';
    } elseif (!verifySuperPassword($config, (string)($_POST['super_password'] ?? ''))) {
        $resetError = '超级密码错误或尚未设置';
    } else {
        $newPassword = (string)($_POST['new_password'] ?? '');
        $confirmPassword = (string)($_POST['confirm_password'] ?? '');
        if (strlen($newPassword) < 10) {
            $resetError = '新后台密码至少需要 10 位';
        } elseif (!hash_equals($newPassword, $confirmPassword)) {
            $resetError = '两次输入的新后台密码不一致';
        } elseif (verifySuperPassword($config, $newPassword)) {
            $resetError = '新后台密码不能与超级密码相同';
        } else {
            try {
                $newPasswordHash = password_hash($newPassword, PASSWORD_DEFAULT);
                if ($newPasswordHash === false) {
                    throw new RuntimeException('后台密码哈希生成失败。');
                }
                $config['admin_password_hash'] = $newPasswordHash;
                $config['admin_password'] = '';
                // 变更后台密码时让所有既有后台会话立即失效。
                $config['admin_session_version'] = bin2hex(random_bytes(32));
                writeAdminConfig($config);
                $_SESSION['password_reset_notice'] = '后台密码已重设，请使用新密码登录';
                redirectSelf();
            } catch (Throwable $e) {
                error_log('Radar admin password reset error: ' . $e->getMessage());
                $resetError = '重设失败，请检查 api 目录写入权限';
            }
        }
    }
    $showForgotPasswordForm = true;
}

if ($method === 'POST' && $action === 'login') {
    if (!isValidCsrfToken($_POST['csrf_token'] ?? null)) {
        $loginError = '页面已失效，请刷新后重试';
    } elseif (verifyAdminPassword($config, (string)($_POST['password'] ?? ''))) {
        session_regenerate_id(true);
        $_SESSION['admin_logged_in'] = true;
        $_SESSION['admin_session_version'] = $adminSessionVersion;
        $_SESSION['csrf_token'] = bin2hex(random_bytes(32));
        redirectSelf();
    } else {
        $loginError = '密码错误';
    }
}

if (empty($_SESSION['admin_logged_in'])) {
    renderHeader($showForgotPasswordForm ? '重设后台密码' : '管理员登录');

    if ($showForgotPasswordForm) {
        if (!empty($resetError)) {
            echo '<div class="msg msg-err">' . h($resetError) . '</div>';
        }
        echo '<div class="card" style="max-width:430px;margin:40px auto"><h2 style="margin-bottom:8px">重设后台密码</h2><p class="muted" style="margin-bottom:15px">验证部署时设置的超级密码后，设置新的后台登录密码。</p>';
        echo '<form method="post" autocomplete="off"><input type="hidden" name="action" value="reset_password"><input type="hidden" name="csrf_token" value="' . h(csrfToken()) . '">';
        echo '<label>超级密码</label><input type="password" name="super_password" required autofocus autocomplete="current-password">';
        echo '<label>新后台密码</label><input type="password" name="new_password" minlength="10" required autocomplete="new-password">';
        echo '<label>确认新后台密码</label><input type="password" name="confirm_password" minlength="10" required autocomplete="new-password">';
        echo '<button type="submit" style="width:100%">验证并重设密码</button></form><p class="muted" style="text-align:center;margin-top:14px"><a href="index.php">返回登录</a></p></div>';
    } else {
        $loginNotice = (string)($_SESSION['password_reset_notice'] ?? '');
        unset($_SESSION['password_reset_notice']);
        if ($loginNotice !== '') {
            echo '<div class="msg msg-ok">' . h($loginNotice) . '</div>';
        }
        if (!empty($loginError)) {
            echo '<div class="msg msg-err">' . h($loginError) . '</div>';
        }
        echo '<div class="card" style="max-width:400px;margin:40px auto"><h2 style="margin-bottom:15px">管理员登录</h2>';
        echo '<form method="post" autocomplete="off"><input type="hidden" name="action" value="login"><input type="hidden" name="csrf_token" value="' . h(csrfToken()) . '">';
        echo '<label>管理密码</label><input type="password" name="password" required autofocus autocomplete="current-password"><button type="submit" style="width:100%">登录</button></form>';
        echo '<p class="muted" style="text-align:center;margin-top:14px"><a href="?forgot=1">忘记后台密码？</a></p></div>';
    }
    renderFooter();
    exit;
}

if ($method === 'POST' && $action === 'logout') {
    if (!isValidCsrfToken($_POST['csrf_token'] ?? null)) {
        setFlash('err', '页面已失效，请刷新后重试');
        redirectSelf();
    }

    $_SESSION = [];
    session_destroy();
    header('Location: index.php', true, 303);
    exit;
}

$message = null;
if ($method === 'POST' && $action !== '') {
    if (!isValidCsrfToken($_POST['csrf_token'] ?? null)) {
        setFlash('err', '页面已失效，请刷新后重试');
        redirectSelf();
    }

    try {
        if ($action === 'generate') {
            $count = max(1, min(100, (int)($_POST['count'] ?? 1)));
            $days = max(1, min(36500, (int)($_POST['duration_days'] ?? 30)));
            $remark = trim((string)($_POST['remark'] ?? ''));
            $remark = function_exists('mb_substr') ? mb_substr($remark, 0, 255, 'UTF-8') : substr($remark, 0, 255);

            $generated = [];
            $insert = $db->prepare('INSERT INTO card_keys (card_key, duration_days, remark) VALUES (?, ?, ?)');
            for ($i = 0; $i < $count; $i++) {
                for ($attempt = 0; $attempt < 10; $attempt++) {
                    $key = generateCardKey();
                    try {
                        $insert->execute([$key, $days, $remark !== '' ? $remark : null]);
                        $generated[] = $key;
                        break;
                    } catch (PDOException $e) {
                        if ((int)($e->errorInfo[1] ?? 0) !== 1062 || $attempt === 9) {
                            throw $e;
                        }
                    }
                }
            }

            $_SESSION['last_generated'] = $generated;
            setFlash('ok', '成功生成 ' . count($generated) . ' 张卡密');
            redirectSelf();
        }

        if (in_array($action, ['ban', 'unban', 'delete'], true)) {
            $keyId = max(0, (int)($_POST['key_id'] ?? 0));
            if ($keyId < 1) {
                throw new RuntimeException('卡密 ID 无效。');
            }

            $db->beginTransaction();
            if ($action === 'ban') {
                $db->prepare("UPDATE card_keys SET status = 'banned' WHERE id = ?")->execute([$keyId]);
                $db->prepare('DELETE FROM sessions WHERE card_key_id = ?')->execute([$keyId]);
                $flashMessage = '已封禁卡密并使现有会话失效';
            } elseif ($action === 'unban') {
                $db->prepare(
                    "UPDATE card_keys
                     SET status = CASE
                         WHEN activated_at IS NULL THEN 'unused'
                         WHEN expires_at IS NOT NULL AND expires_at < NOW() THEN 'expired'
                         ELSE 'active'
                     END
                     WHERE id = ? AND status = 'banned'"
                )->execute([$keyId]);
                $flashMessage = '已解封卡密，原有效期保持不变';
            } else {
                $db->prepare('DELETE FROM card_keys WHERE id = ?')->execute([$keyId]);
                $flashMessage = '已删除卡密';
            }
            $db->commit();
            setFlash('ok', $flashMessage);
            redirectSelf();
        }
    } catch (Throwable $e) {
        if ($db->inTransaction()) {
            $db->rollBack();
        }
        error_log('Radar admin action error: ' . $e->getMessage());
        $message = ['type' => 'err', 'message' => '操作失败，请查看 PHP 错误日志后重试'];
    }
}

expireStaleKeys($db);
purgeExpiredSessions($db);
$keys = $db->query(
    'SELECT ck.*, (SELECT COUNT(*) FROM sessions s WHERE s.card_key_id = ck.id AND s.expires_at > NOW()) AS active_sessions
     FROM card_keys ck
     ORDER BY ck.id DESC
     LIMIT 200'
)->fetchAll();

$flash = $message ?? pullFlash();
renderHeader('卡密管理后台');
echo '<div class="top"><h1>卡密管理后台</h1><form class="inline" method="post"><input type="hidden" name="action" value="logout"><input type="hidden" name="csrf_token" value="' . h(csrfToken()) . '"><button class="btn-muted btn-sm" type="submit">退出</button></form></div>';

if ($flash) {
    $class = ($flash['type'] ?? '') === 'ok' ? 'msg-ok' : 'msg-err';
    echo '<div class="msg ' . $class . '">' . h((string)($flash['message'] ?? '')) . '</div>';
}

if (!empty($_SESSION['last_generated']) && is_array($_SESSION['last_generated'])) {
    echo '<div class="card"><h3>刚生成的卡密（请复制保存）</h3><textarea rows="' . min(10, count($_SESSION['last_generated'])) . '" readonly>';
    echo h(implode("\n", $_SESSION['last_generated']));
    echo '</textarea></div>';
    unset($_SESSION['last_generated']);
}

echo '<div class="card"><h3>批量生成卡密</h3><form method="post" class="flex">';
echo '<input type="hidden" name="action" value="generate"><input type="hidden" name="csrf_token" value="' . h(csrfToken()) . '">';
echo '<div><label>数量</label><input type="number" name="count" value="10" min="1" max="100" required></div>';
echo '<div><label>有效天数</label><input type="number" name="duration_days" value="30" min="1" max="36500" required></div>';
echo '<div><label>备注</label><input type="text" name="remark" maxlength="255" placeholder="可选"></div>';
echo '<div><label>&nbsp;</label><button type="submit">生成</button></div></form></div>';

echo '<div class="card"><h3>卡密列表（最近 200 条）</h3><div class="table-wrap"><table>';
echo '<tr><th>ID</th><th>卡密</th><th>天数</th><th>状态</th><th>激活 / 到期</th><th>有效会话</th><th>备注</th><th>操作</th></tr>';

$statusNames = ['unused' => '未使用', 'active' => '使用中', 'expired' => '已过期', 'banned' => '已封禁'];
foreach ($keys as $key) {
    $status = (string)$key['status'];
    echo '<tr><td>' . (int)$key['id'] . '</td>';
    echo '<td><code>' . h((string)$key['card_key']) . '</code></td>';
    echo '<td>' . (int)$key['duration_days'] . ' 天</td>';
    echo '<td class="status-' . h($status) . '">' . h($statusNames[$status] ?? $status) . '</td>';
    echo '<td>' . ($key['activated_at'] ? h((string)$key['activated_at']) : '-') . '<br>' . ($key['expires_at'] ? h((string)$key['expires_at']) : '-') . '</td>';
    echo '<td>' . (int)$key['active_sessions'] . '</td><td>' . h((string)($key['remark'] ?? '')) . '</td><td>';
    echo '<form class="inline" method="post"><input type="hidden" name="key_id" value="' . (int)$key['id'] . '"><input type="hidden" name="csrf_token" value="' . h(csrfToken()) . '">';
    if ($status === 'banned') {
        echo '<button type="submit" name="action" value="unban" class="btn-sm">解封</button>';
    } else {
        echo '<button type="submit" name="action" value="ban" class="btn-danger btn-sm">封禁</button>';
    }
    echo '</form>';
    echo '<form class="inline" method="post" onsubmit="return confirm(\'确定删除这张卡密？\')"><input type="hidden" name="key_id" value="' . (int)$key['id'] . '"><input type="hidden" name="csrf_token" value="' . h(csrfToken()) . '"><button type="submit" name="action" value="delete" class="btn-danger btn-sm">删除</button></form>';
    echo '</td></tr>';
}

echo '</table></div></div>';
renderFooter();

