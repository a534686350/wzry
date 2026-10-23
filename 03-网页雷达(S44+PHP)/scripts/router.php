<?php
declare(strict_types=1);

$root = dirname(__DIR__);
$uri = parse_url($_SERVER['REQUEST_URI'] ?? '/', PHP_URL_PATH);
$uri = $uri === false || $uri === null ? '/' : rawurldecode($uri);

if ($uri === '/' || $uri === '') {
    $target = is_file($root . '/storage/install.lock') ? '/login.html' : '/install.php';
    header('Location: ' . $target, true, 302);
    return true;
}

if (strpos($uri, '..') !== false || preg_match('#(?:^|/)\.#', $uri) === 1) {
    http_response_code(404);
    echo 'Not Found';
    return true;
}

foreach (['/database/', '/scripts/', '/storage/'] as $blockedPrefix) {
    if (strpos($uri, $blockedPrefix) === 0) {
        http_response_code(404);
        echo 'Not Found';
        return true;
    }
}

$path = $root . str_replace(['/', '\\'], DIRECTORY_SEPARATOR, $uri);
if (is_dir($path)) {
    $indexPhp = $path . DIRECTORY_SEPARATOR . 'index.php';
    $indexHtml = $path . DIRECTORY_SEPARATOR . 'index.html';
    if (is_file($indexPhp) || is_file($indexHtml)) {
        return false;
    }
    http_response_code(404);
    echo 'Not Found';
    return true;
}

if (is_file($path)) {
    return false;
}

http_response_code(404);
echo 'Not Found';
return true;
