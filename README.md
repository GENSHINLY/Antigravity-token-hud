# Token HUD

[![Release](https://img.shields.io/github/v/release/GENSHINLY/Antigravity-token-hud?color=brightgreen)](https://github.com/GENSHINLY/Antigravity-token-hud/releases)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)

基于 PySide6 (Qt 6 单句柄 Alien Widgets 硬件加速渲染) 与 Windows 11 DWM API 实现的 **Antigravity 实时 Token 监控置顶悬浮窗**。开箱即用、自动检测 Antigravity 会话日志，支持 Win11 原生圆角与 Mica 材质及自定义主题，后台独立线程异步采集 JSONL 会话快照，UI 主线程零 I/O 阻塞。

---

## 核心特性

### 1. 开箱即用，自动检测 Antigravity 会话日志
- **零配置自动探测**：启动后自动按优先级探测 Antigravity 会话日志目录（自定义路径 -> `config.json` -> `$GEMINI_HOME/antigravity/brain` -> `~/.gemini/antigravity/brain`）。
- **专注主 Agent 监控**：左侧常驻展示 Antigravity 会话列表，右侧实时展示今日 Token 总用量、综合缓存命中率及输入/缓存/输出/思考 2×2 细分指标；点击任意历史会话即可一键切换查看该对话的详细 Token 消耗、上下文水位与工具调用统计。
- **友好引导提示**：若当前环境尚未产生或未检测到会话日志，界面直接显示引导提示，点击右上角 `[⚙ 设置]` 即可一键自动检测或手动浏览指定路径。

### 2. Win11 原生圆角与 Mica 材质
- **单句柄渲染架构**：全窗口仅顶层 `TokenHUDWindow` 持有原生 `HWND`，内部子控件全部启用 Qt 6 Alien Widgets 硬件合成，消除多句柄闪烁与重排卡顿。
- **Windows 11 原生视觉集成**：通过 `dwmapi.dll` 原生启用沉浸式暗黑模式 (`DWMWA_USE_IMMERSIVE_DARK_MODE`)、系统级圆角 (`DWMWCP_ROUND`) 与 Mica / Mica Alt 材质特效，开启 Per-Monitor DPI 感知。
- **二维响应式防裁切字号**：支持右下角无级拖拽缩放与双栏比例调节，内部大号数字、2×2 指标网格（输入/缓存/输出/思考）及左侧会话列表行高与字号随宽高双向自适应缩放。

### 3. 自定义主题与实时热重载
- **内置 4 套主题预设**：
  - `Gruvbox Material`（默认暖色复古暗黑）
  - `Cyberpunk Dark`（高对比霓虹暗黑）
  - `Catppuccin Mocha`（柔和粉彩暗色）
  - `纯黑 OLED`（纯黑底色低功耗对比）
- **全量 HEX 与拾色器定制**：支持针对主背景 (`bg_color`)、主文字 (`fg_color`)、强调色 (`accent_color`)、卡片背景 (`card_bg_color`)、边框颜色 (`border_color`) 输入 `#RRGGBB` 色值或调用 `QColorDialog` 可视化拾色器。
- **零重启实时热重载**：切换预设、修改色值或调整轮询间隔 (`poll_interval_ms`) 与目录扫描缓存周期 (`scan_ttl_sec`) 后，点击保存即可实时重绘全部组件并动态更新 `QTimer` 周期。

---

## 目录结构

- `token_hud.py`：核心 PySide6 悬浮窗主程序（含 `ConfigManager`、`TokenMonitorCache`、`SettingsDialog`、`TokenHUDWindow` 与 `--self-test` 自检套件）。
- `hud_launcher.cs`：Windows 无控制台窗口启动器源码（C# `winexe` 子系统，支持动态探测脚本路径与单实例唤醒）。
- `build_launcher.ps1`：一键编译 `hud_launcher.cs` 生成本地 `hud.exe` 并同步至 `$env:USERPROFILE\.local\bin\hud.exe` 的构建脚本。
- `README.md`：项目说明文档。

---

## 配置文件 `config.json` 结构与字段说明

程序优先读取同级目录下的 `config.json`，若不存在则回退读取 `~/.gemini/token_hud_config.json`。当通过 `[⚙ 设置]` 面板点击“保存并应用”时，会自动生成或更新该文件。

### 示例配置

```json
{
  "main_agent_path": "",
  "poll_interval_ms": 1000,
  "scan_ttl_sec": 2.5,
  "theme": {
    "preset": "Gruvbox Material",
    "bg_color": "#141617",
    "fg_color": "#D4BE98",
    "accent_color": "#E78A4E",
    "card_bg_color": "#1D2021",
    "border_color": "#3C3836"
  }
}
```

### 字段说明

| 字段路径 | 类型 | 默认值 | 说明 |
| :--- | :--- | :--- | :--- |
| `main_agent_path` | `string` | `""` | Antigravity 主 Agent 日志根目录。留空时自动按环境变量 `GEMINI_HOME` 与 `~/.gemini/antigravity/brain` 探测。 |
| `poll_interval_ms` | `int` | `1000` | 界面快照刷新间隔（毫秒），有效范围 `100` ~ `60000`（设置面板范围 `250` ~ `5000`）。 |
| `scan_ttl_sec` | `float` | `2.5` | 磁盘目录结构重扫缓存周期（秒），有效范围 `0.1` ~ `300.0`（设置面板范围 `1.0` ~ `10.0`）。 |
| `theme.preset` | `string` | `"Gruvbox Material"` | 主题预设名称（`Gruvbox Material` / `Cyberpunk Dark` / `Catppuccin Mocha` / `纯黑 OLED`）。 |
| `theme.bg_color` | `string` | `"#141617"` | 主窗口背景 HEX 色值。 |
| `theme.fg_color` | `string` | `"#D4BE98"` | 主文本前景 HEX 色值。 |
| `theme.accent_color` | `string` | `"#E78A4E"` | 强调高亮 HEX 色值。 |
| `theme.card_bg_color` | `string` | `"#1D2021"` | 统计卡片与侧边栏背景 HEX 色值。 |
| `theme.border_color` | `string` | `"#3C3836"` | 窗口与卡片边框 HEX 色值。 |

---

## 快速上手

### 1. 环境依赖

确保已安装 Python 3.10+ 与 `PySide6`：

```powershell
pip install PySide6
```

### 2. 直接使用 Python 运行

```powershell
# 后台无黑框常驻运行（推荐日常使用）
pyw -3 token_hud.py

# 命令行启动并指定刷新周期
py -3 token_hud.py --interval 1000
```

支持的命令行参数：
- `-s, --session <ID>`：指定优先锁定的会话 ID（前缀或完整 ID）。
- `-p, --project <NAME>`：指定优先过滤的项目目录名。
- `-i, --interval <MS>`：界面刷新间隔毫秒数（默认 `1000`）。
- `--card {0}`：初始展示的卡片索引（`0`=Token 总用量）。
- `--auto-close-idle <SEC>`：监听会话从运行态变为完成态后自动关闭的等待秒数（默认 `0` 常驻）。
- `--duration <SEC>`：运行指定秒数后自动退出（默认 `0` 常驻）。
- `--self-test`：运行非阻塞自动化自检并输出格式化 JSON 报告。

### 3. 使用可视化【⚙ 设置】面板

点击悬浮窗右上角的 `⚙` 按钮（或在右键菜单中选择 `⚙ 设置...`）打开配置窗口：
1. **Agents 监控源**：可点击 `[浏览]` 选择文件夹或点击 `[自动检测]` 验证当前 Antigravity 主 Agent 路径下的会话数量。
2. **外观与配色**：下拉切换 4 套预设主题，或点击色块旁 `[拾色器...]` 实时定制配色。
3. **性能与刷新**：调节轮询间隔与目录扫描缓存 TTL，点击 `[保存并应用]` 立即生效。

### 4. 编译 C# 无黑框启动器 (`hud.exe`) 与快捷拉起

项目提供基于 .NET Framework 原生 `csc.exe` 的零依赖无窗口启动器 `hud_launcher.cs`。执行以下脚本即可编译 `hud.exe` 并自动同步至 `$env:USERPROFILE\.local\bin\hud.exe`：

```powershell
powershell -ExecutionPolicy Bypass -File .\build_launcher.ps1
```

启动器特性：
- **单实例唤醒**：若检测到 `AntigravityTokenHUDMutex` 已存在，自动将后台或最小化的 HUD 窗口恢复显示并激活置顶，不会重复启动进程。
- **动态路径解析**：按 `同级目录 token_hud.py` -> `同级 token_hud_path.txt / config.json` -> `环境变量 TOKEN_HUD_PATH` -> `~/.gemini/token_hud_config.json` -> `编译期源码目录` 顺序自动查找脚本。
- **`Win + R` 快捷拉起（推荐）**：将 `%USERPROFILE%\.local\bin` 加入系统 `PATH` 后，日常推荐直接按 `Win + R` 输入 `hud` 按需快捷拉起（或唤醒置顶）悬浮窗。

---

## 界面交互快捷操作

- **整窗拖拽**：按住顶部标题栏或右侧卡片空白区域可任意拖拽窗口位置。
- **右下角无级缩放**：拖拽窗口右下角斜纹手柄 (`ResizeGripWidget`) 可自由放大或缩小窗口尺寸，内部字号与布局实时自适应。
- **双栏比例调节**：拖拽左栏历史列表与右栏统计卡片之间的纵向分隔条 (`SplitterHandleWidget`)，可在 `18% ~ 65%` 范围内无级调整左右栏宽度占比。
- **千分位明细悬停**：顶部标题栏徽章默认以紧凑格式（如 `12.5M` / `340.2k`）展示今日累计 Token，鼠标悬停其上即可即时切换查看完整千分位精确数值。
- **历史会话回看**：在左栏列表中点击任意 Antigravity 会话即可在主卡片区域查看该对话的 Token 用量与细分指标；点击顶部 `[返回汇总]` 即可回到今日总用量视图。
- **右键快捷菜单**：在窗口任意位置单击右键可呼出菜单，支持：
  - `返回最新活跃会话`
  - `重置窗口大小与位置`（一键恢复默认 `740x330` 尺寸与屏幕右上角对齐）
  - `⚙ 设置...`
  - `立即刷新`
  - `关闭悬浮窗 (Esc)`（也可直接按键盘 `Esc` 键关闭）

---

## 自动化自检 (`--self-test`)

修改代码或验证环境兼容性时，可运行内置的非阻塞自动化自检命令：

```powershell
py -3 token_hud.py --self-test
```

自检流程在独立临时目录中执行（不会污染用户真实配置文件），覆盖并强制断言以下核心模块，全部通过后输出完整 JSON 报告并以退出码 `0` 退出：
- `config_test`：验证 `ConfigManager` 默认值加载、自定义路径持久化、非法脏数据清洗过滤以及损坏 JSON 自动回退。
- `path_detection_test`：验证 `auto_detect_antigravity_path()` 在空目录、自定义路径、直接指定 `transcript.jsonl`、`config.json` 配置及 `GEMINI_HOME` 环境变量下的探测优先级与有效性断言。
- `theme_and_settings_test`：验证 `SettingsDialog` 子 Agent 管理区域隐藏、侧边栏过滤栏常驻隐藏、纯主 Agent 单卡片隔离、4 套主题预设联动、`QColorDialog` 拾色器色值应用、`QTimer` 周期与扫描 TTL 动态更新及无日志友好提示。
- `drag_test` / `resize_test` / `splitter_test` / `dwm_info`：验证窗口拖拽、右下角大/小尺寸双向缩放字号自适应、双栏分隔条拖拽以及 Windows 11 DWM 圆角与 Mica 材质属性返回值。

---

> Built with Google Antigravity.
