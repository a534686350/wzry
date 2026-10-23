<?php
/**
 * 配置示例。
 * 正式部署推荐直接访问根目录 /install.php，由安装器生成 api/config.php。
 */

return [
    'db' => [
        'host'     => '127.0.0.1',
        'port'     => 3306,
        'database' => 'radar_auth',
        'username' => 'radar_user',
        'password' => 'CHANGE_ME',
        'charset'  => 'utf8mb4',
    ],

    // 使用 password_hash('你的后台密码', PASSWORD_DEFAULT) 的输出。
    'admin_password_hash' => '',

    // 仅兼容旧配置；新部署请使用 admin_password_hash。
    'admin_password' => '',

    // 忘记后台密码时验证用的超级密码哈希。
    'super_password_hash' => '',

    // 后台密码变更时轮换此值，以使已登录的后台会话失效。
    'admin_session_version' => '',

    'bind_ip' => false,
    'max_sessions_per_key' => 1,
    'trusted_proxies' => ['127.0.0.1', '::1'],
    'cors_origin' => '',
];
