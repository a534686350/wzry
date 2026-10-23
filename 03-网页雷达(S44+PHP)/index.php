<?php
declare(strict_types=1);

// 免卡密版：不检查安装状态，直接进观战首页。
// 想恢复卡密登录：把下面这行换回原逻辑（见 部署教程.md / api 目录）。
header('Location: /index.html', true, 302);
exit;
