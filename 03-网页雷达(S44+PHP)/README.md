# 03 · 网页雷达（S44 内核 + PHP 站点）

老版观战网页：单文件 `index.html`（约 100 KB，原生 JS）+ layui 后台 + PHP 卡密登录。
**本目录已改成免卡密版**：打开首页直接看，不再要求输入卡密。

## 目录

```
03-网页雷达(S44+PHP)/
├── index.html            观战首页（地图 / 英雄 / 房间列表）
├── index.php             入口：免卡密版直接 302 到 index.html
├── login.html            原卡密登录页（免卡密版用不到，留着以便恢复）
├── admin/index.php       卡密管理后台（免卡密版用不到）
├── api/                  卡密接口：login / logout / verify / ws-auth / db
├── install.php           网页安装器（写 api/config.php + 建库）
├── database/init.sql     卡密库表结构
├── scripts/              安装辅助脚本
├── storage/              安装锁与运行时文件
├── layui/                后台 UI 资源
├── map.png               地图底图（S44 版）
├── .htaccess / .user.ini Apache / PHP 配置
└── 部署教程.md            原版完整部署教程（宝塔从 0 开始）
```

## 免卡密改了哪两处

| 文件 | 改动 |
|---|---|
| `index.php` | 不再判断 `storage/install.lock` 后跳 `login.html`，直接 `302 → index.html` |
| `index.html` | `verifyAuth()` 改为直接放行（右上角显示「公益版 · 免卡密」），不再请求 `api/verify.php`、不再跳登录页 |

`login.html`、`admin/`、`api/`、`database/`、`install.php` 全部原样保留，**想恢复卡密体系**：

```php
// index.php 换回原逻辑
$installed = is_file(__DIR__ . '/storage/install.lock');
header('Location: /' . ($installed ? 'login.html' : 'install.php'), true, 302);
```

再把 `index.html` 里 `verifyAuth()` 换回原来的 fetch 校验（原实现：`api/verify.php` + `Authorization: Bearer <token>`）。

## 部署（免卡密最简版）

只需要 **Nginx + PHP**，不需要 MySQL：

```bash
# 1. 把本目录传到站点根目录，例如 /www/wwwroot/radar
# 2. nginx 增加 /ws 反代到 Java 后端（见 02 目录的 nginx-site.conf.example，删掉 auth_request 那行）
# 3. 启动 Java 后端（02 目录），确认 127.0.0.1:8888 在听
# 4. 浏览器打开 http://你的域名/ → 直接进观战页
```

要卡密 + 后台管理，按 `部署教程.md` 走完整流程（PHP 8.2 + MySQL + `install.php` 安装器）。

## 与 01 目录（新版网页）的区别

| | 01 · dist 前端 | 03 · 本目录 S44 网页 |
|---|---|---|
| 界面 | 新版（Vue 打包，战术地图 + 英雄情报双栏） | 老版（原生 JS，单文件） |
| 数据源 | Python 中转 8080/8888 | Java 后端 8888（走 nginx `/ws`） |
| 授权 | 无 | 无（卡密层已跳过） |
| 地图 | `map.txt.png` / `map_s44.png` | `map.png` |
