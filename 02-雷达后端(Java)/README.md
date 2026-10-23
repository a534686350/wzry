# 02 · 雷达后端（Java）

老版雷达后端，Netty WebSocket 服务，**S44 内核**的数据源。网页雷达（`03-网页雷达(S44+PHP)`）
和安卓客户端连的都是它。

## 文件

| 文件 | 说明 |
|---|---|
| `home-server-0.0.1-SNAPSHOT.jar` | 后端主程序（6.0 MB，3820 个类，Netty） |
| `radar-ws.service` | systemd 单元模板（把 `DOMAIN` 换成你的站点目录） |
| `nginx-site.conf.example` | 宝塔 nginx 片段：`/ws` 反代 + 卡密 Cookie 校验 |

## 环境

| 项 | 要求 |
|---|---|
| JDK | **8 及以上**（编译于 1.8.0_31，class major 52） |
| 内存 | 512 MB 起（`-Xmx1024M` 足够） |
| 端口 | **8888/tcp**（只对本机开放，由 nginx 反代 `/ws` 出去） |
| 主类 | `com.qy.homeserver.main.NettyEchoServer` |

## 启动

```bash
# 直接跑
java -jar -Xmx1024M -Xms256M /www/wwwroot/你的站点/home-server-0.0.1-SNAPSHOT.jar

# 或 systemd
sed 's|DOMAIN|你的站点目录|g' radar-ws.service > /etc/systemd/system/radar-ws.service
systemctl daemon-reload && systemctl enable --now radar-ws
ss -lntp | grep 8888
```

## 与网页雷达的配合

```
浏览器 ──https://你的域名/ws──> nginx ──反代──> 127.0.0.1:8888 (本 Java 服务)
```

- nginx 片段见 `nginx-site.conf.example`，`/ws` 必须带 WebSocket 升级头；
- 原来还有一层 `auth_request /api/ws-auth.php` 卡密校验，**免卡密版可以删掉这行**（见 03 目录说明）；
- 8888 不要直接暴露到公网，走 nginx 反代即可（APP 直连场景除外）。

## 与 01（Python 中转）的区别

| | 01 Python 中转（推荐） | 02 Java 后端（本目录） |
|---|---|---|
| 语言 | Python 3.6+，单文件 | Java 8+，Netty |
| 端口 | 8080 网页 / 8888 数据 / 9999 上报 | 8888 WebSocket |
| 网页 | 内置 dist 前端（新版 UI） | 配 03 的 S44 网页 |
| 采集 | TCP 9999 推帧 | 客户端直连 8888 |
| 授权 | 无 | 无（配套的 PHP 卡密已改免卡密） |
| 适合 | 新部署、多级子站、一键部署 | 沿用老架构、老客户端 |

两套不冲突，选一套用就行；也可以 Python 中转跑新版网页、Java 后端跑老版网页，各占不同端口。
