<?php
declare(strict_types=1);

/**
 * 为已安装站点初始化或更换“超级密码”。
 * 用法：/www/server/php/82/bin/php /www/wwwroot/DOMAIN/scripts/set-super-password.php
 */
if (PHP_SAPI !== 'cli') {
    http_response_code(403);
    exit("CLI only.\n");
}

$configPath = dirname(__DIR__) . '/api/config.php';
if (!is_file($configPath)) {
    fwrite(STDERR, "未找到 api/config.php，请先完成网站安装。\n");
    exit(1);
}

$config = require $configPath;
if (!is_array($config)) {
    fwrite(STDERR, "api/config.php 格式无效。\n");
    exit(1);
}

function readPassword(string $prompt): string
{
    fwrite(STDOUT, $prompt);
    $value = fgets(STDIN);
    if ($value === false) {
        fwrite(STDERR, "读取密码失败。\n");
        exit(1);
    }

    return rtrim($value, "\r\n");
}

$superPassword = readPassword('请输入新的超级密码（至少 10 位）：');
$confirmation = readPassword('请再次输入超级密码：');

if (strlen($superPassword) < 10) {
    fwrite(STDERR, "超级密码至少需要 10 位。\n");
    exit(1);
}
if (!hash_equals($superPassword, $confirmation)) {
    fwrite(STDERR, "两次输入的超级密码不一致。\n");
    exit(1);
}

$adminHash = trim((string)($config['admin_password_hash'] ?? ''));
$legacyAdminPassword = (string)($config['admin_password'] ?? '');
if (($adminHash !== '' && password_verify($superPassword, $adminHash))
    || ($legacyAdminPassword !== '' && hash_equals($legacyAdminPassword, $superPassword))) {
    fwrite(STDERR, "超级密码不能与当前后台管理密码相同。\n");
    exit(1);
}

$hash = password_hash($superPassword, PASSWORD_DEFAULT);
if ($hash === false) {
    fwrite(STDERR, "超级密码哈希生成失败。\n");
    exit(1);
}

$config['super_password_hash'] = $hash;
$contents = "<?php\ndeclare(strict_types=1);\n\nreturn " . var_export($config, true) . ";\n";
$temporaryPath = $configPath . '.tmp';

if (file_put_contents($temporaryPath, $contents, LOCK_EX) === false || !@rename($temporaryPath, $configPath)) {
    @unlink($temporaryPath);
    fwrite(STDERR, "写入 api/config.php 失败，请检查 api 目录所有者和权限。\n");
    exit(1);
}

@chmod($configPath, 0640);
fwrite(STDOUT, "超级密码已保存。现在可通过 /admin/?forgot=1 重设后台密码。\n");