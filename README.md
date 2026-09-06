# NCP-ControlPanel — USB 小屏系统监控

在 CH554 USB 小屏（沁恒 CH554_CDC，480×320 横向面板，USB VID:PID `1a86:5722`）上实时显示**横版**监控面板：

- CPU：总负载（大数字）、频率、温度
- 内存：占用率、已用/总量
- GPU：型号（RTX 3080）、显存容量/占用、负载、温度、功耗、风扇
- NCP：自定义服务与隧道进程的运行状态（监控目标在 `local_config.py` 中配置，
  该文件不入库；示例见 `local_config.example.py`）

屏幕未接时程序自动以预览窗口运行；接入后自动开始向小屏推送画面（脏区域增量刷新）。

## 横版方向

程序按 **480×320 横版**渲染，发送时旋转到串口的 320×480 竖屏坐标系（固件再转回面板）。
把屏幕横着放置后如果画面是倒的，换一个方向参数：

```bash
--rotate 270   # 默认。对应"屏幕逆时针转90°放置"（USB 线在右侧）
--rotate 90    # 对应"屏幕顺时针转90°放置"（USB 线在左侧）
```

## 运行

依赖：`python3 -m pip install --user pyserial psutil numpy pillow nvidia-ml-py`（本机已装好）。

```bash
# 带桌面预览窗口运行（预览关掉则程序退出）
python3 mini_screen_monitor.py

# 无预览常驻（推荐配合 systemd）
python3 mini_screen_monitor.py --no-preview

# 其他选项
--port /dev/ttyACM0    # 指定串口（默认自动按 VID/PID 查找）
--interval 2           # 刷新间隔秒（默认 2）
--dump out.png         # 渲染一帧存 PNG 退出（调试布局）
--test-pattern         # 向屏发送彩条测试图（验证方向和颜色）
```

首次运行需串口权限：已安装 `/etc/udev/rules.d/99-ch554-mini-screen.rules`
（MODE 0660 + GROUP dialout + uaccess），并把用户加入 dialout 组。

## 开机自启（systemd 用户服务）

```bash
mkdir -p ~/.config/systemd/user
cp ncp-controlpanel.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now ncp-controlpanel
journalctl --user -u ncp-controlpanel -f     # 看日志
```

## 串口协议（逆向自 Windows 上位机"主题选择器"）

- 串口参数 115200 8N1（CH554 的 CDC 波特率是虚拟的，实际速率取决于 USB，约 50KB/s）
- 绘制坐标系为 **320×480 竖屏**，固件内部旋转到 480×320 横向面板
- 每个刷新区域先发 60 字节头，再连续发 BGR 像素（60 字节一包，末包补 `0x00`）：

```
[x_hi x_lo] [y_hi y_lo] [w_hi w_lo] [h_hi h_lo] FC FC 01 00×49
^大端16位坐标  ^大端16位区域大小   ^魔数  ^模式1=RGB888(BGR序)
```

- 上位机逐帧 diff，只发送变化的瓦片区域；全屏一帧约 46 万字节 ≈ 9 秒，
  增量刷新（只变更数字/条形）每帧仅几 KB，秒级完成。

## 文件

- `mini_screen_monitor.py` — 主程序（单文件）
- `ncp-controlpanel.service` — systemd 用户服务单元
- `fonts/` — FX-LED 数码管时钟字体、simhei 兜底中文字体（来自原 Windows 上位机主题包）
- `/etc/udev/rules.d/99-ch554-mini-screen.rules` — 串口 udev 规则
