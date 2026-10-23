-- 王者雷达卡密系统表结构。
-- 此文件不创建数据库，也不切换数据库：请通过 /install.php 或在目标数据库中导入。

CREATE TABLE IF NOT EXISTS `card_keys` (
    `id` INT UNSIGNED NOT NULL AUTO_INCREMENT,
    `card_key` VARCHAR(32) NOT NULL COMMENT '卡密',
    `duration_days` INT UNSIGNED NOT NULL DEFAULT 30 COMMENT '有效天数（首次激活后开始计算）',
    `status` ENUM('unused', 'active', 'expired', 'banned') NOT NULL DEFAULT 'unused' COMMENT '状态',
    `activated_at` DATETIME NULL COMMENT '首次激活时间',
    `expires_at` DATETIME NULL COMMENT '到期时间',
    `bind_ip` VARCHAR(45) NULL COMMENT '绑定 IP（可选）',
    `remark` VARCHAR(255) NULL COMMENT '备注',
    `created_at` DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (`id`),
    UNIQUE KEY `uk_card_key` (`card_key`),
    KEY `idx_status` (`status`),
    KEY `idx_expires_at` (`expires_at`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='卡密表';

CREATE TABLE IF NOT EXISTS `sessions` (
    `id` INT UNSIGNED NOT NULL AUTO_INCREMENT,
    `card_key_id` INT UNSIGNED NOT NULL,
    `token` VARCHAR(64) NOT NULL COMMENT '登录令牌',
    `ip_address` VARCHAR(45) NULL,
    `user_agent` VARCHAR(512) NULL,
    `created_at` DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    `expires_at` DATETIME NOT NULL COMMENT '会话到期时间',
    `last_active` DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (`id`),
    UNIQUE KEY `uk_token` (`token`),
    KEY `idx_card_key_id` (`card_key_id`),
    KEY `idx_expires_at` (`expires_at`),
    CONSTRAINT `fk_sessions_card_key` FOREIGN KEY (`card_key_id`) REFERENCES `card_keys` (`id`) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='登录会话表';
