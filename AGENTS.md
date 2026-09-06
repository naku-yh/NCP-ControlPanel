# AGENTS.md — AI 编码代理须知

本仓库是运行在用户 Linux 主机（i7-11700F + RTX 3080，Ubuntu 22.04）上的
**USB 小屏系统监控仪表盘**，仓库/程序在用户机器上直接开发、直接部署运行。
改动代码前请通读本文，尤其是"硬件与协议"一节——协议是从 Windows 上位机
逆向得到的，不要凭猜测修改。

## 项目结构

```
mini_screen_monitor.py    主程序（单文件，含采集/渲染/串口驱动/预览/CLI）
ncp-controlpanel.service  systemd 用户服务单元
fonts/                    FX-LED.TTF（时钟数码管字体）、simhei_.ttf（中文兜底）
README.md                 使用文档
```

## 硬件与串口协议（逆向结论，勿凭直觉改动）

- 硬件：沁恒 CH554_CDC 虚拟串口小屏，USB VID:PID `1a86:5722`，设备 `/dev/ttyACM0`，
  面板 480×320 横向。串口参数 115200 8N1 —— **波特率是虚拟的**，实际吞吐由 USB
  决定（约 50KB/s，全屏一帧 ≈ 9 秒），不要试图"提高波特率提速"。
- 坐标系：串口线上是 **320 宽 × 480 高的竖屏空间**，固件内部旋转到横向面板。
- 每个刷新区域的发送格式：先 60 字节头
  `[x_hi x_lo y_hi y_lo w_hi w_lo h_hi h_lo] + FC FC 01 + 00×49`（坐标/宽高均为
  大端 16 位），随后按行连续发送该区域 **BGR** 像素（RGB 的 R/B 互换），每
  60 字节一包，末包不足补 `0x00`。
- 上位机负责逐帧 diff、只发变化区域（`diff_regions()`，20px 瓦片 + 行内合并，
  区域数超过 80 退化为全屏重发）。
- 渲染流程：`render()` 画 **480×320 横版** → `to_wire()` 按用户放置方向
  `--rotate 270`（默认）或 `90` 旋转成竖屏 → diff → 发送。

## 数据源与外部依赖

- CPU/内存/温度：psutil；CPU 温度取 `coretemp` 的 `Package id 0`。
- GPU：pynvml（NVML），取索引 0；名称去掉 "NVIDIA GeForce " 前缀显示。
- **NCP 是用户自定义的监控项**（一个本地服务 + 一个隧道进程的运行状态）：
  - 服务状态 = TCP 端口连通 + 周期性 HTTP 健康检查；
  - 隧道状态 = `pgrep -f <进程名>`。
  - 真实地址/端口/进程名含内网信息，**只存在于本机不入库的
    `local_config.py`**（占位示例见 `local_config.example.py`）。
    严禁把 `local_config.py` 的内容写进入库文件、提交信息或任何公开渠道；
    新增涉及内网信息的配置一律走 local_config 机制。

## 运行环境要点

- Python 3.10，依赖 `pyserial psutil numpy pillow nvidia-ml-py`（本机已
  `pip install --user` 装好）。
- 串口权限靠 udev 规则 `/etc/udev/rules.d/99-ch554-mini-screen.rules`
  （MODE 0666，插拔后立即可用），**不要用 sudo 运行程序**。
- 程序有单实例文件锁（`/tmp/mini-screen-monitor.lock`）；测试前先停旧实例：
  先 `pgrep -f mini_screen_monitor` 拿 PID 再 `kill <PID>`。
  **注意：`pkill -f mini_screen_monitor` 会匹配到执行它的 shell 自身并误杀。**
- 用户桌面是 GNOME（DISPLAY=:0），`--no-preview` 不加时会开 pygame 预览窗。

## 验证改动的顺序

1. 布局/数据渲染：`python3 mini_screen_monitor.py --no-preview --dump /tmp/x.png`
   退出后查看 PNG（不碰串口，不需要停常驻实例）。
2. 方向/颜色验证：`--test-pattern` 发彩条测试图，需用户目视确认。
3. 真机常驻：`--no-preview` 后台运行；串口拔插会自动重连（重连后整屏重绘）。
4. 涉及画面布局的改动，最终以用户在实体屏上的观感为准——渲染 PNG 验收通过
   不代表实体屏效果（面板色彩、亮度与设计稿有差异）。

## 约定

- 界面文案与注释用中文；新配置项集中放在主程序顶部 CONFIG 区。
- **本仓库公开**：严禁提交任何 IP、端口、域名、SSH 地址、主机名等内网/个人
  信息；本机专属配置放 `local_config.py`（已被 .gitignore 忽略）。提交前用
  git grep 自查：IPv4 地址、4~5 位端口号、SSH 远程地址均应无匹配。
- 保持单文件结构；确有必要拆分时先征询用户。
- 字体：主用系统 Noto Sans CJK；`fonts/FX-LED.TTF`（时钟数码管）来自用户自有的
  Windows 主题包。`fonts/simhei_.ttf` 是可选兜底字体，**不入库**（商业字体，
  已被 .gitignore 忽略），缺失时自动跳过。
- 提交信息用中文、一句话说清改动（`git log` 风格参照现有提交）。
