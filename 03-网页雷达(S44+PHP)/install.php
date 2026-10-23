<?php
declare(strict_types=1);

$root = __DIR__;
$configPath = $root . '/api/config.php';
$schemaPath = $root . '/database/init.sql';
$storagePath = $root . '/storage';
$lockPath = $storagePath . '/install.lock';

$https = (!empty($_SERVER['HTTPS']) && $_SERVER['HTTPS'] !== 'off')
    || (int)($_SERVER['SERVER_PORT'] ?? 80) === 443;
session_name('radar_install');
session_set_cookie_params([
    'lifetime' => 0,
    'path' => '/',
    'secure' => $https,
    'httponly' => true,
    'samesite' => 'Strict',
]);
session_start();

function e(string $value): string
{
    return htmlspecialchars($value, ENT_QUOTES | ENT_SUBSTITUTE, 'UTF-8');
}

function installCsrfToken(): string
{
    if (empty($_SESSION['install_csrf_token'])) {
        $_SESSION['install_csrf_token'] = bin2hex(random_bytes(32));
    }

    return (string)$_SESSION['install_csrf_token'];
}

function validInstallCsrf(?string $token): bool
{
    return is_string($token)
        && !empty($_SESSION['install_csrf_token'])
        && hash_equals((string)$_SESSION['install_csrf_token'], $token);
}

function splitSqlStatements(string $sql): array
{
    $sql = preg_replace('/^\s*(?:--|#).*$/m', '', $sql) ?? $sql;
    $statements = [];
    $buffer = '';
    $quote = null;
    $escaped = false;
    $length = strlen($sql);

    for ($index = 0; $index < $length; $index++) {
        $character = $sql[$index];
        $buffer .= $character;

        if ($quote !== null) {
            if ($escaped) {
                $escaped = false;
            } elseif ($character === '\\' && $quote !== '`') {
                $escaped = true;
            } elseif ($character === $quote) {
                $quote = null;
            }
            continue;
        }

        if ($character === "'" || $character === '"' || $character === '`') {
            $quote = $character;
            continue;
        }

        if ($character === ';') {
            $statement = trim(substr($buffer, 0, -1));
            if ($statement !== '') {
                $statements[] = $statement;
            }
            $buffer = '';
        }
    }

    $tail = trim($buffer);
    if ($tail !== '') {
        $statements[] = $tail;
    }

    return $statements;
}

function writeProjectConfig(string $path, array $config): void
{
    $contents = "<?php\ndeclare(strict_types=1);\n\nreturn " . var_export($config, true) . ";\n";
    $temporaryPath = $path . '.tmp';
    if (file_put_contents($temporaryPath, $contents, LOCK_EX) === false) {
        throw new RuntimeException('无法写入临时配置文件。');
    }

    if (!@rename($temporaryPath, $path)) {
        @unlink($temporaryPath);
        throw new RuntimeException('无法替换 api/config.php；请检查网站目录写入权限。');
    }

    @chmod($path, 0640);
}

/**
 * 安装前实际创建并删除测试文件，提前发现 storage/install.lock 的权限错误，
 * 避免在数据库建表和配置写入后才报权限异常。
 */
function verifyWritableDirectory(string $directory): bool
{
    $temporaryPath = @tempnam($directory, '.radar-install-');
    if ($temporaryPath === false) {
        return false;
    }

    $written = @file_put_contents($temporaryPath, "write-test\n", LOCK_EX) !== false;
    $removed = @unlink($temporaryPath);

    return $written && $removed;
}

function renderInstallPage(array $values, array $errors = [], ?string $success = null, bool $alreadyInstalled = false): void
{
    $disabled = $alreadyInstalled ? ' disabled' : '';
    echo '<!DOCTYPE html><html lang="zh-CN"><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>王者雷达安装</title>';
    echo '<style>*{box-sizing:border-box}body{margin:0;min-height:100vh;padding:28px 16px;font-family:-apple-system,BlinkMacSystemFont,"Segoe UI","Microsoft YaHei",sans-serif;background:#0f1419;color:#edf2f7}.wrap{max-width:720px;margin:auto}.card{background:#18212b;border:1px solid #2d3c4b;border-radius:16px;padding:28px;box-shadow:0 16px 50px rgba(0,0,0,.25)}h1{margin:0 0 8px;color:#4aa3ff}.sub{color:#aab7c4;margin:0 0 24px}.grid{display:grid;grid-template-columns:1fr 1fr;gap:14px}.full{grid-column:1/-1}label{display:block;color:#b9c5d1;font-size:14px;margin-bottom:6px}input{width:100%;border:1px solid #385064;border-radius:8px;background:#0d141b;color:#fff;padding:11px 12px;font-size:15px}input:focus{outline:2px solid #277dc9;border-color:#277dc9}.check{display:flex;align-items:center;gap:8px;margin-top:6px}.check input{width:auto}.btn{margin-top:22px;width:100%;border:0;border-radius:8px;background:#258bea;color:#fff;padding:12px;font-size:16px;cursor:pointer}.btn:hover{background:#1577cf}.btn:disabled{opacity:.55;cursor:not-allowed}.notice,.error,.success{border-radius:8px;padding:12px 14px;margin:0 0 16px}.notice{background:#172c3c;border:1px solid #315e7a;color:#c6e6fb}.error{background:#3b1f24;border:1px solid #a34b58;color:#ffd8dd}.success{background:#183829;border:1px solid #3d9765;color:#d8ffea}ul{margin:8px 0 0;padding-left:20px}.tip{font-size:13px;color:#99a7b5;margin-top:4px}@media(max-width:620px){.grid{grid-template-columns:1fr}.full{grid-column:auto}.card{padding:20px}}</style></head><body><main class="wrap"><section class="card">';
    echo '<h1>王者雷达安装</h1><p class="sub">填写宝塔数据库中创建好的数据库名、用户名和密码；安装器会写入配置并创建卡密表。</p>';

    if ($alreadyInstalled) {
        echo '<div class="success">安装已完成。为防止覆盖线上配置，安装器已锁定。<br><br><a href="/login.html" style="color:#8ed0ff">进入用户登录页</a>　<a href="/admin/" style="color:#8ed0ff">进入卡密管理后台</a></div>';
    } elseif ($success !== null) {
        echo '<div class="success">' . e($success) . '<br><br><a href="/admin/" style="color:#8ed0ff">现在进入卡密管理后台</a>　<a href="/login.html" style="color:#8ed0ff">进入用户登录页</a></div>';
    } else {
        echo '<div class="notice">开始前：请先在宝塔「数据库」中添加数据库和数据库用户。当前数据库用户至少需要该数据库的建表、查询、插入、更新和删除权限。</div>';
        if ($errors !== []) {
            echo '<div class="error"><strong>安装未完成：</strong><ul>';
            foreach ($errors as $error) {
                echo '<li>' . e($error) . '</li>';
            }
            echo '</ul></div>';
        }
        echo '<form method="post" autocomplete="off"><input type="hidden" name="csrf_token" value="' . e(installCsrfToken()) . '"><div class="grid">';
        echo '<div><label for="database">数据库名</label><input id="database" name="database" maxlength="64" required value="' . e($values['database']) . '"' . $disabled . '><p class="tip">仅支持字母、数字和下划线。</p></div>';
        echo '<div><label for="username">数据库用户名</label><input id="username" name="username" maxlength="64" required value="' . e($values['username']) . '"' . $disabled . '></div>';
        echo '<div><label for="password">数据库密码</label><input id="password" type="password" name="password" required autocomplete="new-password"' . $disabled . '></div>';
        echo '<div><label for="admin_password">后台管理密码</label><input id="admin_password" type="password" name="admin_password" minlength="10" required autocomplete="new-password"' . $disabled . '><p class="tip">至少 10 位，用于 /admin/ 登录。</p></div>';
        echo '<div class="full"><label for="super_password">超级密码</label><input id="super_password" type="password" name="super_password" minlength="10" required autocomplete="new-password"' . $disabled . '><p class="tip">忘记后台密码时，用于验证并重设后台密码；请与后台管理密码分开保存。</p></div>';
        echo '<div class="full"><label for="host">数据库主机</label><input id="host" name="host" required value="' . e($values['host']) . '"' . $disabled . '><p class="tip">宝塔本机 MySQL 保持 127.0.0.1；端口默认 3306。</p></div>';
        echo '<div class="full check"><input id="bind_ip" type="checkbox" name="bind_ip" value="1"' . ($values['bind_ip'] ? ' checked' : '') . $disabled . '><label for="bind_ip" style="margin:0">首登后绑定卡密 IP</label></div>';
        echo '</div><button class="btn" type="submit"' . $disabled . '>开始安装</button></form>';
    }

    echo '</section></main></body></html>';
}

$values = [
    'database' => trim((string)($_POST['database'] ?? 'radar_auth')),
    'username' => trim((string)($_POST['username'] ?? '')),
    'host' => trim((string)($_POST['host'] ?? '127.0.0.1')),
    'bind_ip' => !empty($_POST['bind_ip']),
];

if (is_file($lockPath)) {
    renderInstallPage($values, [], null, true);
    exit;
}

if (($_SERVER['REQUEST_METHOD'] ?? 'GET') !== 'POST') {
    renderInstallPage($values);
    exit;
}

$errors = [];
if (!validInstallCsrf($_POST['csrf_token'] ?? null)) {
    $errors[] = '页面已失效，请刷新后重新提交。';
}
if (!preg_match('/^[A-Za-z0-9_]{1,64}$/', $values['database'])) {
    $errors[] = '数据库名只能包含字母、数字和下划线，长度不超过 64 位。';
}
if ($values['username'] === '' || strlen($values['username']) > 64) {
    $errors[] = '请填写有效的数据库用户名。';
}
if (!preg_match('/^[A-Za-z0-9._:-]{1,255}$/', $values['host'])) {
    $errors[] = '数据库主机格式不正确。';
}
$password = (string)($_POST['password'] ?? '');
$adminPassword = (string)($_POST['admin_password'] ?? '');
$superPassword = (string)($_POST['super_password'] ?? '');
if ($password === '') {
    $errors[] = '请填写数据库密码。';
}
if (strlen($adminPassword) < 10) {
    $errors[] = '后台管理密码至少需要 10 位。';
}
if (strlen($superPassword) < 10) {
    $errors[] = '超级密码至少需要 10 位。';
} elseif ($superPassword === $adminPassword) {
    $errors[] = '超级密码请与后台管理密码设置为不同的值。';
}
if (!extension_loaded('pdo_mysql')) {
    $errors[] = '当前 PHP 未启用 pdo_mysql 扩展，请在宝塔 PHP 管理器中安装该扩展。';
}
if (!is_file($schemaPath)) {
    $errors[] = '找不到 database/init.sql。';
}
if (!is_writable(dirname($configPath)) || !verifyWritableDirectory(dirname($configPath))) {
    $errors[] = 'api 目录不可写，请在宝塔文件管理中把网站目录所有者设为 www。';
}
if (!is_dir($storagePath) && !@mkdir($storagePath, 0755, true) && !is_dir($storagePath)) {
    $errors[] = '无法创建 storage 目录，请在宝塔文件管理中把网站目录所有者设为 www。';
} elseif (!is_writable($storagePath) || !verifyWritableDirectory($storagePath)) {
    $errors[] = 'storage 目录不可写，无法创建 install.lock。请执行 chown -R www:www 网站根目录，并将 storage 权限设为 775。';
}

if ($errors !== []) {
    renderInstallPage($values, $errors);
    exit;
}

try {
    $pdo = new PDO(
        'mysql:host=' . $values['host'] . ';port=3306;dbname=' . $values['database'] . ';charset=utf8mb4',
        $values['username'],
        $password,
        [
            PDO::ATTR_ERRMODE => PDO::ERRMODE_EXCEPTION,
            PDO::ATTR_EMULATE_PREPARES => false,
        ]
    );

    $schema = file_get_contents($schemaPath);
    if ($schema === false) {
        throw new RuntimeException('无法读取 database/init.sql。');
    }
    foreach (splitSqlStatements($schema) as $statement) {
        $pdo->exec($statement);
    }

    $config = [
        'db' => [
            'host' => $values['host'],
            'port' => 3306,
            'database' => $values['database'],
            'username' => $values['username'],
            'password' => $password,
            'charset' => 'utf8mb4',
        ],
        'admin_password_hash' => password_hash($adminPassword, PASSWORD_DEFAULT),
        'admin_password' => '',
        'super_password_hash' => password_hash($superPassword, PASSWORD_DEFAULT),
        // 变更后台密码时轮换该值，使旧后台会话全部失效。
        'admin_session_version' => bin2hex(random_bytes(32)),
        'bind_ip' => $values['bind_ip'],
        'max_sessions_per_key' => 1,
        'trusted_proxies' => ['127.0.0.1', '::1'],
        'cors_origin' => '',
    ];
    writeProjectConfig($configPath, $config);

    $lock = json_encode(['installed_at' => gmdate('c')], JSON_UNESCAPED_UNICODE) . "\n";
    if (@file_put_contents($lockPath, $lock, LOCK_EX) === false) {
        throw new RuntimeException('安装成功但无法写入安装锁，请执行 chown -R www:www 网站根目录，并将 storage 权限设为 775。');
    }
    @chmod($lockPath, 0640);
    unset($_SESSION['install_csrf_token']);

    renderInstallPage($values, [], '数据库连接成功，卡密表与后台配置已创建。请按部署教程配置 Nginx 的 /ws 反向代理并启动 Java 服务。');
} catch (Throwable $e) {
    error_log('Radar installation error: ' . $e->getMessage());
    renderInstallPage($values, ['数据库连接、建表或写入配置失败。请核对数据库名、用户名、密码、权限及 PHP 错误日志。']);
}
