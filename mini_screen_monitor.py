#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
mini_screen_monitor.py — USB 小屏系统监控仪表盘 (Linux)

目标硬件: CH554_CDC 虚拟串口小屏 (USB VID:PID = 1a86:5722, 480x320 横向面板)
串口协议(逆向自 Windows 上位机"主题选择器"):
  * 串口 115200 8N1
  * 坐标系为 320x480 竖屏空间(固件内部旋转到横向面板)
  * 刷新区域 = 先发 60 字节头:
      [x_hi, x_lo, y_hi, y_lo, w_hi, w_lo, h_hi, h_lo] + FC FC 01 + 0x00*49
  * 再把该区域 BGR 像素按行连续发送, 每 60 字节一包, 末包不足补 0x00
  * 上位机只发送与上一帧相比发生变化的区域

显示内容: CPU 负载/温度/频率, 内存占用, GPU 型号/显存/负载/温度/功耗/风扇,
          NCP 服务与隧道进程运行状态(监控目标经 local_config.py 配置, 不入库)
"""

import argparse
import datetime as dt
import fcntl
import os
import socket
import struct
import subprocess
import sys
import time
import urllib.request

import numpy as np
import psutil
from PIL import Image, ImageDraw, ImageFont

try:
    import serial
    import serial.tools.list_ports
except ImportError:
    serial = None

# ----------------------------------------------------------------------------
# 配置
# ----------------------------------------------------------------------------
VID, PID = 0x1A86, 0x5722          # CH554_CDC
BAUD = 115200
PKT = 60                            # 每包字节数(固件按 60 字节解析)
W, H = 320, 480                     # 串口线上的竖屏坐标系, 固件负责旋转
LW, LH = 480, 320                   # 实际显示的横版画布
TILE = 20                           # 脏区域检测瓦片尺寸

# ---- NCP 监控目标 ----------------------------------------------------------
# 真实地址/端口/进程名含内网信息, 不入库: 复制 local_config.example.py 为
# local_config.py 并按实际情况修改; 未提供时使用下面的通用占位默认值。
try:
    from local_config import NCP_HOST, NCP_PORT, NCP_HTTP_PATH, TUNNEL_PROC
except ImportError:
    NCP_HOST = "localhost"          # NCP 服务地址
    NCP_PORT = 8080                 # NCP 服务端口
    NCP_HTTP_PATH = "/health"       # HTTP 健康检查路径, 留空跳过检查
    TUNNEL_PROC = ""                # 隧道客户端进程名, 留空则不检测

NCP_HTTP_URL = (f"http://{NCP_HOST}:{NCP_PORT}{NCP_HTTP_PATH}"
                if NCP_HTTP_PATH else None)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
FONT_DIR = os.path.join(BASE_DIR, "fonts")

# ----------------------------------------------------------------------------
# 串口屏驱动
# ----------------------------------------------------------------------------
class Screen:
    def __init__(self):
        self.ser = None
        self.port_name = None

    @staticmethod
    def find_port(preferred=None):
        if preferred:
            return preferred
        if serial is None:
            return None
        try:
            for p in serial.tools.list_ports.comports():
                if p.vid == VID and p.pid == PID:
                    return p.device
        except Exception:
            pass
        return None

    def connect(self, preferred=None):
        if serial is None:
            return False
        port = self.find_port(preferred)
        if not port:
            return False
        try:
            self.ser = serial.Serial(port, BAUD, bytesize=8, parity="N",
                                     stopbits=1, timeout=0.2, exclusive=True)
            self.port_name = port
            return True
        except Exception as e:
            print(f"[screen] 打开 {port} 失败: {e}", file=sys.stderr)
            self.ser = None
            return False

    def close(self):
        if self.ser:
            try:
                self.ser.close()
            except Exception:
                pass
        self.ser = None
        self.port_name = None

    @property
    def connected(self):
        return self.ser is not None and self.ser.is_open

    def send_region(self, x, y, w, h, bgr):
        """发送一个矩形区域: 60字节头 + BGR像素(60字节/包, 末包补零)"""
        head = struct.pack(">8B", (x >> 8) & 0xFF, x & 0xFF,
                           (y >> 8) & 0xFF, y & 0xFF,
                           (w >> 8) & 0xFF, w & 0xFF,
                           (h >> 8) & 0xFF, h & 0xFF)
        head += b"\xFC\xFC\x01" + b"\x00" * 49
        assert len(head) == PKT
        self.ser.write(head)
        n = len(bgr)
        for off in range(0, n, PKT):
            chunk = bgr[off:off + PKT]
            if len(chunk) < PKT:
                chunk = chunk + b"\x00" * (PKT - len(chunk))
            self.ser.write(chunk)

    def send_image(self, img):
        """全屏发送(PIL Image, 320x480 RGB)"""
        self.send_region(0, 0, W, H, image_to_bgr(img))


def image_to_bgr(img):
    arr = np.asarray(img, dtype=np.uint8)
    return np.ascontiguousarray(arr[:, :, ::-1]).tobytes()


def diff_regions(prev_arr, new_arr):
    """比较前后帧, 返回需要重发的矩形区域列表 [(x, y, w, h), ...]"""
    if prev_arr is None:
        return [(0, 0, W, H)]
    changed = np.any(prev_arr != new_arr, axis=2)
    gh, gw = H // TILE, W // TILE
    regions = []
    for gy in range(gh):
        row = changed[gy * TILE:(gy + 1) * TILE, :]
        run_start = None
        for gx in range(gw + 1):
            dirty = gx < gw and bool(row[:, gx * TILE:(gx + 1) * TILE].any())
            if dirty and run_start is None:
                run_start = gx
            elif not dirty and run_start is not None:
                regions.append((run_start * TILE, gy * TILE,
                                (gx - run_start) * TILE, TILE))
                run_start = None
    if len(regions) > 80:
        return [(0, 0, W, H)]
    return regions

# ----------------------------------------------------------------------------
# 数据采集
# ----------------------------------------------------------------------------
_nvml = {"ok": None, "h": None}

def gpu_info():
    """返回 dict: name/vram_total/vram_used/util/temp/fan/power, 不可用时为 None"""
    if _nvml["ok"] is None:
        try:
            import pynvml
            pynvml.nvmlInit()
            _nvml["h"] = pynvml
            _nvml["ok"] = True
        except Exception:
            _nvml["ok"] = False
    if not _nvml["ok"]:
        return None
    try:
        pynvml = _nvml["h"]
        h = pynvml.nvmlDeviceGetHandleByIndex(0)
        name = pynvml.nvmlDeviceGetName(h)
        if isinstance(name, bytes):
            name = name.decode()
        mem = pynvml.nvmlDeviceGetMemoryInfo(h)
        util = pynvml.nvmlDeviceGetUtilizationRates(h)
        temp = pynvml.nvmlDeviceGetTemperature(h, pynvml.NVML_TEMPERATURE_GPU)
        try:
            fan = pynvml.nvmlDeviceGetFanSpeed(h)
        except Exception:
            fan = None
        try:
            power = pynvml.nvmlDeviceGetPowerUsage(h) / 1000.0
        except Exception:
            power = None
        return {"name": name, "vram_total": mem.total, "vram_used": mem.used,
                "util": util.gpu, "temp": temp, "fan": fan, "power": power}
    except Exception:
        # 驱动可能重载过, 下次循环重新初始化
        try:
            _nvml["h"].nvmlShutdown()
        except Exception:
            pass
        _nvml["ok"] = None
        return None


def tcp_open(host, port, timeout=0.4):
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def http_ok(url, timeout=1.5):
    try:
        req = urllib.request.Request(url, method="GET")
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return 200 <= r.status < 500
    except Exception:
        return False


def tunnel_running():
    if not TUNNEL_PROC:
        return False
    try:
        return subprocess.run(["pgrep", "-f", TUNNEL_PROC],
                              stdout=subprocess.DEVNULL,
                              stderr=subprocess.DEVNULL).returncode == 0
    except Exception:
        return False


def collect(cycle):
    d = {}
    d["cpu"] = psutil.cpu_percent(interval=None)
    d["cores"] = psutil.cpu_percent(interval=None, percpu=True)
    try:
        f = psutil.cpu_freq()
        d["cpu_ghz"] = f.current / 1000.0 if f else None
    except Exception:
        d["cpu_ghz"] = None
    d["cpu_temp"] = None
    try:
        temps = psutil.sensors_temperatures() or {}
        for key in ("coretemp", "k10temp", "cpu_thermal", "acpitz"):
            if key in temps and temps[key]:
                entries = [t for t in temps[key] if "Package" in (t.label or "")] \
                          or temps[key]
                d["cpu_temp"] = entries[0].current
                break
    except Exception:
        pass

    vm = psutil.virtual_memory()
    d["ram_pct"] = vm.percent
    d["ram_used"] = vm.used
    d["ram_total"] = vm.total

    d["gpu"] = gpu_info()

    d["ncp_up"] = tcp_open(NCP_HOST, NCP_PORT)
    if NCP_HTTP_URL and d["ncp_up"] and cycle % 10 == 0:
        d["ncp_http"] = http_ok(NCP_HTTP_URL)
    else:
        d["ncp_http"] = None
    d["tunnel_up"] = tunnel_running()
    d["uptime"] = time.time() - psutil.boot_time()
    return d

# ----------------------------------------------------------------------------
# 渲染
# ----------------------------------------------------------------------------
BG = (13, 17, 23)
CARD = (22, 27, 34)
CARD_EDGE = (48, 54, 61)
TRACK = (33, 38, 45)
TXT = (230, 237, 243)
DIM = (139, 148, 158)
CYAN = (88, 196, 255)
GREEN = (63, 185, 80)
ORANGE = (210, 153, 34)
RED = (248, 81, 73)


def temp_color(t):
    if t is None:
        return DIM
    return GREEN if t < 60 else (ORANGE if t < 80 else RED)


def load_color(pct):
    return GREEN if pct < 60 else (ORANGE if pct < 85 else RED)


_font_cache = {}

def font(kind, size):
    key = (kind, size)
    if key in _font_cache:
        return _font_cache[key]
    paths = {
        "reg": ["/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
                os.path.join(FONT_DIR, "simhei_.ttf")],
        "bold": ["/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc",
                 os.path.join(FONT_DIR, "simhei_.ttf")],
        "led": [os.path.join(FONT_DIR, "FX-LED.TTF"),
                "/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc"],
    }[kind]
    f = None
    for p in paths:
        try:
            idx = 2 if p.endswith(".ttc") else 0   # ttc 第2项 = 简体中文
            f = ImageFont.truetype(p, size, index=idx)
            break
        except Exception:
            continue
    if f is None:
        f = ImageFont.load_default()
    _font_cache[key] = f
    return f


def gb(nbytes, nd=1):
    return f"{nbytes / 1024 ** 3:.{nd}f}"


def bar(dr, x, y, w, h, pct, color, radius=3):
    dr.rounded_rectangle([x, y, x + w, y + h], radius=radius, fill=TRACK)
    fw = max(2, int(w * min(100, max(0, pct)) / 100))
    if pct > 0:
        dr.rounded_rectangle([x, y, x + fw, y + h], radius=radius, fill=color)


def dot(dr, x, y, r, color):
    dr.ellipse([x - r, y - r, x + r, y + r], fill=color)


def render(d):
    """绘制 480x320 横版画面(实际显示方向), 由调用方旋转到串口坐标系"""
    img = Image.new("RGB", (LW, LH), BG)
    dr = ImageDraw.Draw(img)

    now = dt.datetime.now()
    host = socket.gethostname()
    weekday = "周一 周二 周三 周四 周五 周六 周日".split()[now.weekday()]

    # ---- 顶部 ----------------------------------------------------------
    dr.text((14, 5), host, font=font("bold", 14), fill=TXT)
    up = d["uptime"]
    dr.text((14, 21),
            f"{now.strftime('%m-%d')} {weekday} · "
            f"已运行 {int(up // 86400)}天{int(up % 86400 // 3600)}时",
            font=font("reg", 12), fill=DIM)
    dr.text((LW - 12, 2), now.strftime("%H:%M:%S"), font=font("led", 26),
            fill=CYAN, anchor="ra")
    dr.line([10, 38, LW - 10, 38], fill=CARD_EDGE, width=1)

    # ---- CPU(左) --------------------------------------------------------
    cx, cy, cw, ch = 8, 44, 228, 150
    dr.rounded_rectangle([cx, cy, cx + cw, cy + ch], radius=8,
                         fill=CARD, outline=CARD_EDGE, width=1)
    dr.text((cx + 12, cy + 8), "CPU", font=font("bold", 18), fill=CYAN)
    dr.text((cx + 58, cy + 12), "i7-11700F", font=font("reg", 12), fill=DIM)
    dr.text((cx + cw - 12, cy + 26), f"{d['cpu']:.0f}%",
            font=font("bold", 46), fill=load_color(d["cpu"]), anchor="ra")

    sub = []
    if d["cpu_ghz"]:
        sub.append(f"{d['cpu_ghz']:.2f} GHz")
    sub.append(f"{d['cpu_temp']:.0f}°C"
               if d["cpu_temp"] is not None else "--°C")
    dr.text((cx + 12, cy + 84), " · ".join(sub), font=font("bold", 15),
            fill=temp_color(d["cpu_temp"]))
    bar(dr, cx + 12, cy + 112, cw - 24, 14, d["cpu"],
        load_color(d["cpu"]))

    # ---- GPU(右) --------------------------------------------------------
    gx, gy = 244, 44
    dr.rounded_rectangle([gx, gy, gx + cw, gy + ch], radius=8,
                         fill=CARD, outline=CARD_EDGE, width=1)
    dr.text((gx + 12, gy + 8), "GPU", font=font("bold", 18), fill=CYAN)
    g = d["gpu"]
    if g:
        gname = g["name"].replace("NVIDIA GeForce ", "").replace("NVIDIA ", "")
        dr.text((gx + 58, gy + 12), gname, font=font("bold", 13), fill=TXT)
        vram_t = g["vram_total"] / 1024 ** 2   # MiB
        vram_u = g["vram_used"] / 1024 ** 2
        vram_pct = vram_u / vram_t * 100 if vram_t else 0
        dr.text((gx + 12, gy + 40), "显存", font=font("reg", 13), fill=DIM)
        dr.text((gx + cw - 12, gy + 38),
                f"{vram_u / 1024:.1f} / {vram_t / 1024:.0f} GB",
                font=font("bold", 15), fill=TXT, anchor="ra")
        bar(dr, gx + 12, gy + 60, cw - 24, 12, vram_pct, CYAN)
        stats = [("负载", f"{g['util']:.0f}%", load_color(g["util"])),
                 ("温度", f"{g['temp']:.0f}°C", temp_color(g["temp"])),
                 ("功耗", f"{g['power']:.0f}W"
                  if g["power"] is not None else "--", TXT),
                 ("风扇", f"{g['fan']:.0f}%"
                  if g["fan"] is not None else "--", TXT)]
        col_w = (cw - 24) / 4
        for i, (label, val, color) in enumerate(stats):
            x = gx + 12 + i * col_w
            dr.text((x, gy + 86), label, font=font("reg", 12), fill=DIM)
            dr.text((x, gy + 104), val, font=font("bold", 19), fill=color)
    else:
        dr.text((gx + 12, gy + 60), "未检测到 NVIDIA GPU",
                font=font("reg", 14), fill=DIM)

    # ---- 内存(整行) -----------------------------------------------------
    ry = 200
    dr.rounded_rectangle([8, ry, 8 + 464, ry + 46], radius=8,
                         fill=CARD, outline=CARD_EDGE, width=1)
    dr.text((20, ry + 6), "内存", font=font("bold", 16), fill=CYAN)
    bar(dr, 74, ry + 15, 220, 14, d["ram_pct"], load_color(d["ram_pct"]))
    dr.text((306, ry + 12), f"{gb(d['ram_used'])} / {gb(d['ram_total'])} GB",
            font=font("reg", 13), fill=DIM)
    dr.text((460, ry + 8), f"{d['ram_pct']:.0f}%", font=font("bold", 22),
            fill=load_color(d["ram_pct"]), anchor="ra")

    # ---- NCP(整行) ------------------------------------------------------
    ny = 252
    dr.rounded_rectangle([8, ny, 8 + 464, ny + 46], radius=8,
                         fill=CARD, outline=CARD_EDGE, width=1)
    rows = [("NCP 服务", d["ncp_up"], 240),
            ("NCP 隧道", d["tunnel_up"], 464)]
    if d["ncp_http"] is False:
        rows[0] = ("NCP 服务·无响应", False, 240)
    for label, up_flag, right in rows:
        color = GREEN if up_flag else RED
        text = "运行中" if up_flag else "已停止"
        left = right - 224
        dot(dr, left + 8, ny + 23, 5, color)
        dr.text((left + 20, ny + 13), label, font=font("reg", 15), fill=TXT)
        dr.text((right - 8, ny + 12), text, font=font("bold", 16),
                fill=color, anchor="ra")

    # ---- 底部 ----------------------------------------------------------
    dr.text((LW // 2, LH - 6), f"更新 {now.strftime('%H:%M:%S')}",
            font=font("reg", 10), fill=DIM, anchor="mm")
    return img

# ----------------------------------------------------------------------------
# 预览窗口
# ----------------------------------------------------------------------------
class Preview:
    SCALE = 2

    def __init__(self):
        try:
            import pygame
            pygame.init()
            info = pygame.display.Info()
            scale = self.SCALE
            while LH * scale > info.current_h * 0.95 and scale > 1:
                scale -= 1
            self.pygame = pygame
            self.win = pygame.display.set_mode((LW * scale, LH * scale))
            pygame.display.set_caption("小屏监控预览 (480x320 横版)")
            self.scale = scale
        except Exception as e:
            print(f"[preview] 无法打开预览窗口: {e}", file=sys.stderr)
            self.pygame = None

    def show(self, img):
        if not self.pygame:
            return False
        import pygame
        for e in pygame.event.get():
            if e.type == pygame.QUIT:
                return False
        surf = pygame.surfarray.make_surface(
            np.asarray(img).swapaxes(0, 1))
        if self.scale != 1:
            surf = pygame.transform.scale(
                surf, (LW * self.scale, LH * self.scale))
        self.win.blit(surf, (0, 0))
        pygame.display.flip()
        return True

    def quit(self):
        if self.pygame:
            self.pygame.quit()

# ----------------------------------------------------------------------------
# 主循环
# ----------------------------------------------------------------------------
def prime_sensors():
    psutil.cpu_percent(interval=None)
    psutil.cpu_percent(interval=None, percpu=True)
    time.sleep(0.15)
    psutil.cpu_percent(interval=None)
    psutil.cpu_percent(interval=None, percpu=True)


def make_test_image():
    """测试图: 彩条 + 网格 + 文字, 用于验证屏显方向和色彩 (480x320 横版)"""
    img = Image.new("RGB", (LW, LH), (0, 0, 0))
    dr = ImageDraw.Draw(img)
    colors = [(255, 0, 0), (0, 255, 0), (0, 0, 255), (255, 255, 0),
              (0, 255, 255), (255, 0, 255), (255, 255, 255), (128, 128, 128)]
    bw = LW / len(colors)
    for i, c in enumerate(colors):
        dr.rectangle([i * bw, 0, (i + 1) * bw, 56], fill=c)
    for gx in range(0, LW, 40):
        dr.line([gx, 64, gx, LH], fill=(40, 80, 40), width=1)
    for gy in range(64, LH, 40):
        dr.line([0, gy, LW, gy], fill=(40, 80, 40), width=1)
    dr.text((LW // 2, 120), "CH554 屏幕测试 480x320 横版",
            font=font("bold", 30), fill=(255, 255, 255), anchor="mm")
    dr.text((LW // 2, 170), "115200 · BGR · 60B/包",
            font=font("reg", 20), fill=(88, 196, 255), anchor="mm")
    dr.text((LW // 2, 240), "CPU OK ▶ 123.4%  RAM OK",
            font=font("bold", 26), fill=(63, 185, 80), anchor="mm")
    dr.text((LW // 2, 285), "GPU OK ▶ NCP OK ▶ 2026-09-06",
            font=font("bold", 26), fill=(210, 153, 34), anchor="mm")
    return img


_LOCK_FP = None

def acquire_lock():
    """防止多个实例同时写串口。文件对象必须全程持有, 否则锁立即失效。"""
    global _LOCK_FP
    lock_path = "/tmp/mini-screen-monitor.lock"
    fp = open(lock_path, "w")
    try:
        fcntl.flock(fp, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        print("已有实例在运行 (先停止它: pgrep -f mini_screen_monitor 查 PID"
              " 后 kill, 或 systemctl --user stop ncp-controlpanel)",
              file=sys.stderr)
        sys.exit(1)
    _LOCK_FP = fp


def main():
    ap = argparse.ArgumentParser(description="USB 小屏系统监控仪表盘 (横版)")
    ap.add_argument("--port", help="指定串口 (默认按 VID/PID 自动查找)")
    ap.add_argument("--interval", type=float, default=2.0, help="刷新间隔秒")
    ap.add_argument("--no-preview", action="store_true", help="不打开预览窗口")
    ap.add_argument("--no-serial", action="store_true", help="不连接小屏")
    ap.add_argument("--dump", metavar="PNG", help="渲染一帧保存为 PNG 后退出")
    ap.add_argument("--test-pattern", action="store_true",
                    help="向小屏发送测试图后退出")
    ap.add_argument("--rotate", type=int, choices=(90, 270), default=270,
                    help="屏幕横放方向: 270=逆时针转90°放置(USB线在右侧), "
                         "90=顺时针转90°放置(USB线在左侧)。装反了换另一个值")
    args = ap.parse_args()

    rot = Image.Transpose.ROTATE_270 if args.rotate == 270 \
        else Image.Transpose.ROTATE_90

    def to_wire(img_land):
        """横版画面 -> 串口 320x480 竖屏坐标系"""
        return img_land.transpose(rot)

    if not args.dump:
        acquire_lock()
    if args.test_pattern:
        scr = Screen()
        if not scr.connect(args.port):
            print("未找到小屏串口", file=sys.stderr)
            sys.exit(1)
        print(f"[screen] {scr.port_name} 发送测试图 ...")
        scr.send_image(to_wire(make_test_image()))
        time.sleep(1)
        scr.close()
        print("完成")
        return

    prime_sensors()
    prev_arr = None
    cycle = 0
    scr = Screen()
    last_try = 0.0

    preview = None
    if not args.no_preview:
        preview = Preview()

    if args.dump:
        d = collect(0)
        render(d).save(args.dump)
        print(f"已保存 {args.dump}")
        return

    print("Ctrl+C 退出")
    try:
        while True:
            t0 = time.time()
            d = collect(cycle)
            img_land = render(d)
            arr = np.asarray(to_wire(img_land), dtype=np.uint8)

            if preview:
                if not preview.show(img_land):
                    preview.quit()
                    preview = None

            if not args.no_serial:
                if not scr.connected and time.time() - last_try > 5:
                    last_try = time.time()
                    if scr.connect(args.port):
                        print(f"[screen] 已连接 {scr.port_name}")
                        prev_arr = None       # 重连后整屏重绘
                if scr.connected:
                    try:
                        for (x, y, w, h) in diff_regions(prev_arr, arr):
                            crop = arr[y:y + h, x:x + w]
                            bgr = np.ascontiguousarray(crop[:, :, ::-1]) \
                                       .tobytes()
                            scr.send_region(x, y, w, h, bgr)
                        prev_arr = arr
                    except Exception as e:
                        print(f"[screen] 发送失败: {e}", file=sys.stderr)
                        scr.close()

            cycle += 1
            delay = args.interval - (time.time() - t0)
            if delay > 0:
                time.sleep(delay)
    except KeyboardInterrupt:
        pass
    finally:
        if preview:
            preview.quit()
        scr.close()


if __name__ == "__main__":
    main()
