<?php
/**
 * 数据库与系统配置。
 * 首次部署请访问根目录 /install.php，由安装器自动写入真实配置。
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

    // 安装器会写入 password_hash() 生成的哈希。保留 admin_password 仅用于兼容旧配置。
    'admin_password_hash' => '',
    'admin_password' => '',

    // 忘记后台登录密码时，用此密码验证后重设后台密码。
    'super_password_hash' => '',

    // 后台密码变更时轮换此值，以使已登录的后台会话失效。
    'admin_session_version' => '',

    // true 时，卡密首次登录后绑定客户端 IP。
    'bind_ip' => false,

    // 每张卡密最多的有效会话数；0 表示不限制。
    'max_sessions_per_key' => 1,

    // 仅当 REMOTE_ADDR 位于此列表时，才信任 X-Forwarded-For / X-Real-IP。
    'trusted_proxies' => ['127.0.0.1', '::1'],

    // 留空表示仅同源请求；需要跨域时填写单个源或来源数组，例如 https://example.com。
    'cors_origin' => '',
];
