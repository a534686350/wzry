#!/bin/bash
# ============================================
#  王者视界雷达 · 一键安装脚本
#  用法：bash install.sh [安装目录] [网页端口] [数据WS端口] [上报TCP端口]
#  例：  bash install.sh /www/wwwroot/king 8080 8888 9999
#
#  端口分工（三者不能混用）：
#    网页端口  8080  前台/后台/部署页，浏览器打开就是它
#    数据WS    8888  APP 客户端与网页拉房间数据（APP 端写死 8888，别改）
#    上报TCP   9999  采集端上报对局帧
# ============================================
set -e

DIR="${1:-/www/wwwroot/king}"
WEB="${2:-8080}"
PORT="${3:-8888}"
TCP="${4:-9999}"
SRC="$(cd "$(dirname "$0")/.." && pwd)"

echo "==> 安装目录: $DIR"
echo "==> 网页端口: $WEB"
echo "==> 数据 WS 端口: $PORT"
echo "==> 客户端上报端口: $TCP"

# 1. python3 检查
if ! command -v python3 >/dev/null 2>&1; then
  echo "!! 未检测到 python3，尝试安装…"
  (apt-get update -qq && apt-get install -y python3) >/dev/null 2>&1 || \
  (yum install -y python3) >/dev/null 2>&1 || true
fi
if ! command -v python3 >/dev/null 2>&1; then
  echo "!! python3 安装失败，请手动装好再跑"; exit 1
fi
echo "==> python3: $(python3 -V 2>&1)"

# 2. 端口占用检查（本项目自己的旧实例不算，service 会被覆盖重启）
for P in "$WEB" "$PORT" "$TCP"; do
  if ss -lnt 2>/dev/null | awk '{print $4}' | grep -q ":$P\$"; then
    echo "!! 端口 $P 已被占用。若是本项目旧实例，先 systemctl stop king-relay-lite 再跑；"
    echo "   若是别的程序，请换端口：bash install.sh $DIR $WEB $PORT $TCP"
    exit 1
  fi
done

# 3. 拷贝文件
mkdir -p "$DIR"
cp -r "$SRC/dist" "$DIR/" 2>/dev/null || true
cp "$SRC/king-relay-lite.py" "$DIR/"
if [ ! -f "$DIR/config.json" ]; then
  cp "$SRC/config.example.json" "$DIR/config.json"
  chmod 600 "$DIR/config.json"
  echo "==> 已生成 config.json（首次启动会自动生成管理密码，见日志）"
fi

# 4. systemd
cat > /etc/systemd/system/king-relay-lite.service <<EOF
[Unit]
Description=King Vision Relay
After=network.target

[Service]
Type=simple
WorkingDirectory=$DIR
Environment=PYTHONUNBUFFERED=1
Environment=WEB_PORT=$WEB
Environment=WS_PORT=$PORT
Environment=TCP_PORT=$TCP
Environment=STATIC_DIR=$DIR/dist
Environment=ROOM_TTL=60
ExecStart=/usr/bin/python3 $DIR/king-relay-lite.py
Restart=always
RestartSec=3

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable king-relay-lite >/dev/null 2>&1 || true
systemctl restart king-relay-lite
sleep 3

# 5. 结果
ST=$(systemctl is-active king-relay-lite || true)
CODE=$(curl -s -o /dev/null -m 5 -w '%{http_code}' "http://127.0.0.1:$WEB/" || true)
echo ""
echo "============================================"
echo " systemd    : $ST"
echo " 网页 HTTP  : $CODE"
echo " 网页       : http://本机IP:$WEB/"
echo " 后台       : http://本机IP:$WEB/admin"
echo " 数据 WS    : 本机IP:$PORT   （APP 填这个）"
echo " 上报 TCP   : 本机IP:$TCP"
echo "============================================"
if [ "$ST" != "active" ] || [ "$CODE" != "200" ]; then
  echo "启动异常，看日志：journalctl -u king-relay-lite -n 40 --no-pager"
  exit 1
fi
echo "管理密码（首次启动生成，只打印一次）："
journalctl -u king-relay-lite -n 60 --no-pager | grep -A2 "管理密码" | tail -3 || \
  grep -o '"admin_pass": *"[^"]*"' "$DIR/config.json"
echo ""
echo "云服务器记得在安全组放行 $WEB/tcp（网页）、$PORT/tcp（数据）、$TCP/tcp（上报）"
