"""完全按 APP（com.hai）的握手与订阅流程，验证本站 relay 能否服务它。"""
import base64, os, socket, struct, sys, time

HOST, PORT = "YOUR_SERVER_IP", 8888
ROOM = sys.argv[1] if len(sys.argv) > 1 else "603"


def frame(text):
    data = text.encode()
    mask = os.urandom(4)
    ln = len(data)
    head = bytes([0x81])
    if ln < 126:
        head += bytes([0x80 | ln])
    elif ln < 65536:
        head += bytes([0x80 | 126]) + struct.pack("!H", ln)
    else:
        head += bytes([0x80 | 127]) + struct.pack("!Q", ln)
    body = bytes(b ^ mask[i & 3] for i, b in enumerate(data))
    return head + mask + body


def read_frame(sock):
    h = sock.recv(2)
    if len(h) < 2:
        return None
    b1, b2 = h[0], h[1]
    ln = b2 & 0x7F
    if ln == 126:
        ln = struct.unpack("!H", sock.recv(2))[0]
    elif ln == 127:
        ln = struct.unpack("!Q", sock.recv(8))[0]
    data = b""
    while len(data) < ln:
        chunk = sock.recv(ln - len(data))
        if not chunk:
            break
        data += chunk
    return b1 & 0x0F, data


s = socket.create_connection((HOST, PORT), timeout=10)
key = base64.b64encode(os.urandom(16)).decode()
s.sendall(("GET /ws HTTP/1.1\r\nHost: %s:%d\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
           "Sec-WebSocket-Key: %s\r\nSec-WebSocket-Version: 13\r\n\r\n" % (HOST, PORT, key)).encode())
time.sleep(0.5)
head = s.recv(4096).decode("latin1")
print("handshake:", head.splitlines()[0])

# 1) room-probe：subscribeHome 看房间存在
s.sendall(frame("subscribeHome"))
time.sleep(0.6)
op, data = read_frame(s)
print("homeData:", data.decode("utf-8", "ignore")[:200])

# 2) 订阅房间
s.sendall(frame("subscribe[==]" + ROOM))
got = 0
deadline = time.time() + 6
while time.time() < deadline:
    r = read_frame(s)
    if not r:
        break
    op, data = r
    txt = data.decode("utf-8", "ignore")
    if txt.startswith("gameData##"):
        got += 1
        if got == 1:
            print("gameData len:", len(txt) - 10, "head:", txt[10:110])
        if got >= 3:
            break

# 3) 心跳（APP 用毫秒时间戳）
s.sendall(frame("ping##%d" % int(time.time() * 1000)))
time.sleep(0.5)
op, data = read_frame(s)
print("ping reply:", data.decode("utf-8", "ignore")[:40])
print("gameData frames:", got)
s.close()
