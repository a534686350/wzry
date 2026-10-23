<?php
declare(strict_types=1);

/**
 * 命令行数据库初始化工具。
 * 用法：php scripts/install-db.php <project-root> <config-path>
 */

if (PHP_SAPI !== 'cli') {
    http_response_code(403);
    exit('CLI only' . PHP_EOL);
}

if ($argc < 3) {
    fwrite(STDERR, "Usage: php scripts/install-db.php <project-root> <config-path>" . PHP_EOL);
    exit(1);
}

$root = realpath($argv[1]);
$configPath = $argv[2];
if ($root === false || !is_dir($root)) {
    fwrite(STDERR, "Project root not found: {$argv[1]}" . PHP_EOL);
    exit(1);
}
if (!is_file($configPath)) {
    fwrite(STDERR, "Config file not found: {$configPath}" . PHP_EOL);
    exit(1);
}
if (!extension_loaded('pdo_mysql')) {
    fwrite(STDERR, "Missing required PHP extension: pdo_mysql" . PHP_EOL);
    exit(1);
}

$config = require $configPath;
$db = is_array($config) ? ($config['db'] ?? null) : null;
if (!is_array($db)) {
    fwrite(STDERR, "Invalid config format." . PHP_EOL);
    exit(1);
}

$host = (string)($db['host'] ?? '127.0.0.1');
$port = (int)($db['port'] ?? 3306);
$database = (string)($db['database'] ?? '');
$username = (string)($db['username'] ?? '');
$password = (string)($db['password'] ?? '');
$charset = (string)($db['charset'] ?? 'utf8mb4');
if (!preg_match('/^[A-Za-z0-9_]{1,64}$/', $database)) {
    fwrite(STDERR, "Invalid database name in config." . PHP_EOL);
    exit(1);
}

try {
    echo "[1/3] Connecting to configured database..." . PHP_EOL;
    $pdo = new PDO(
        sprintf('mysql:host=%s;port=%d;dbname=%s;charset=%s', $host, $port, $database, $charset),
        $username,
        $password,
        [PDO::ATTR_ERRMODE => PDO::ERRMODE_EXCEPTION, PDO::ATTR_EMULATE_PREPARES => false]
    );

    $sqlPath = $root . DIRECTORY_SEPARATOR . 'database' . DIRECTORY_SEPARATOR . 'init.sql';
    $sql = is_file($sqlPath) ? file_get_contents($sqlPath) : false;
    if ($sql === false) {
        throw new RuntimeException("SQL file not found or unreadable: {$sqlPath}");
    }

    echo "[2/3] Importing schema..." . PHP_EOL;
    foreach (splitSqlStatements($sql) as $statement) {
        $pdo->exec($statement);
    }

    echo "[3/3] Database ready." . PHP_EOL;
    echo "OK" . PHP_EOL;
} catch (Throwable $e) {
    fwrite(STDERR, "[FAILED] " . $e->getMessage() . PHP_EOL);
    exit(1);
}

function splitSqlStatements(string $sql): array
{
    $sql = preg_replace('/^\s*(?:--|#).*$/m', '', $sql) ?? $sql;
    $statements = [];
    $buffer = '';
    $quote = null;
    $escaped = false;

    for ($index = 0, $length = strlen($sql); $index < $length; $index++) {
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
        } elseif ($character === ';') {
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
