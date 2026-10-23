# 强哥公益雷达同款雷达 · 免授权版（2026-10-23）

自研中转服务 + 前台/后台站点 + 安卓客户端。纯 Python 标准库，单进程三端口，**无任何授权码/卡密/到期限制**。

> 完整操作步骤看 **`使用文档.md`**；本文件只讲目录、端口与技术要点。

---

## 一、目录

```
源码1023-免授权版/              完整源码（三套实现）
├── 使用文档.md               部署 / 配置 / 网页 / APP / 排错 全流程
├── king-relay-lite.py        服务端主程序（3,934 行，LICENSE_OFF=True）
├── config.example.json       配置模板（首启自动复制成 config.json）
├── data/
│   ├── license.json.example  授权码模板（本版不校验）
│   └── nodes.json.example    部署记录模板
├── deploy/
│   ├── install.sh            systemd 一键安装
│   └── nginx.conf            80 端口反代示例
├── tools/
│   └── sim_app.py            模拟 APP 的 WS 客户端（验证握手/订阅链路）
├── docs/
│   └── 协议差异-S44vs1.2.8.md   两套内核的帧格式差异
├── 02-雷达后端(Java)/      Java 后端（Netty 8888，JDK 8+，6.0 MB jar + systemd + nginx 片段）
├── 03-网页雷达(S44+PHP)/   S44 老版观战网页（单文件 index.html + layui，已改免卡密）
└── dist/
    ├── index/                新版前台
    │   ├── index.html
    │   ├── assets/index-noauth.js   免授权构建（域名锁/赞助页已删）
    │   ├── assets/index-ClEQBCMH.css
    │   ├── map.txt.png       地图底图（必留）
    │   ├── map_s44.png       S44 简化版底图（可切换）
    │   └── share-preview.jpg / share-thumb.jpg
    └── app.apk               安卓客户端（322 KB，免云后台）
```

> 原厂后台静态 SPA（`dist/admin/`，含 china.json / ChinaMap 3.7 MB）**已移除**：
> 服务端 `/admin` 一律走 Python 内置页面，该目录本来就被 404 拦截，属于死资源。

---

## 二、端口模型

| 端口 | 用途 | 谁连 |
|---|---|---|
| 8080 | 网页 / 后台 / `/deploy` / `/app.apk` / 网页用 WebSocket | 浏览器 |
| 8888 | 游戏数据 WebSocket（房间列表、对局帧） | APP、采集端、子站镜像 |
| 9999 | 客户端上报（裸 TCP） | 采集端 |
| 80 | nginx 反代 8080（可选） | 浏览器 |

8888 收到普通 HTTP 会 **302 跳到 8080**，旧链接不会白屏。APP 端端口写死 8888，两边必须一致。

---

## 三、部署

```bash
tar xzf 源码1023-免授权版.zip && cd 源码1023-免授权版
bash deploy/install.sh /www/wwwroot/king 8080 8888 9999
```

云服务器安全组放行 **8080 / 8888 / 9999** 三个 TCP。

---

## 四、技术要点

- **免授权**：`LICENSE_OFF = True`，6 处校验短路（部署卡密、授权码、远程复查、下发配置、403 拦截、复查线程）。改回 `False` 即恢复原授权体系
- **双内核自适应**：S44 老内核（10 字段纯数字帧）与 1.2.8 新内核（16~17 字段）自动识别互转
- **老内核必须请求-响应轮询**：S44 服务端只推一次，30ms 泵线程 ≈32fps 持续拉
- **上游镜像**：子站连母站 WS 同步房间列表，按需订阅对局帧，断线 3s 重连（退避至 30s）
- **一键部署**：后台填 SSH（或 `/deploy` 自助页）→ 装 python3 → 上传 → 写 systemd → 起服务 → 验端口；三端口三层写死
- **Python 3.6~3.12 兼容**：`asyncio.run` / `create_task` / `serve_forever` 三处按版本降级
- **前台免授权构建**：原混淆包已还原为可读源码，删除域名白名单校验、`/support`、`/purchase` 赞助授权页、顶栏「搭建同款」按钮，`auth.seayun.cn` 引用清零
- **APP 免授权**：云后台配置请求替换为内置本地配置，反代理检测恒通过，`云后台/验证` 文案中性化；
  **无内置默认服务器**，只能填自己的 IP / 域名，端口固定 8888

---

## 五、与线上服务器版的差异

| 项 | 线上母站 YOUR_SERVER_IP | 本包 |
|---|---|---|
| `king-relay-lite.py` | 179,306 B / 3,885 行 / md5 `a2d1344d…` | 177,911 B / 3,934 行 / md5 `138ca528…` |
| 授权 | 完整校验 | `LICENSE_OFF = True` |
| 母站地址 | 写死 `YOUR_SERVER_IP` | `PUBLIC_HOST` 环境变量 / 自动探测 |
| 前台 JS | `index-MdrsxC1t.js`（含域名锁+赞助页） | `index-noauth.js`（已清理） |
| `dist/admin/` | 原厂后台 SPA | 已移除 |
| `dist/app.apk` | `427e81cf…`（含云后台校验） | 重建版（免云后台） |

差异 22 个改动块 / 92 行，明细见 `使用文档.md` 第九节。

---

## 六、说明

- 所有服务器地址均已占位符化，部署时填自己的。
- `data/nodes.json` 会存子站 SSH 明文密码，勿外传、勿进公开仓库。
- 本包为免授权构建，分发不受限；如需限制，把 `LICENSE_OFF` 改回 `False` 重新启用卡密体系。
