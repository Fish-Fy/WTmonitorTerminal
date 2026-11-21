# WTMonitorTerminal · 战雷战术小地图终端

Windows 桌面战术面板，汇总 War Thunder 本地 `127.0.0.1:8111` 接口提供的地图、击杀文案与飞行仪表数据，支持地图标点和手机浏览器显示。

[Full English README](#english)

## 下载与启动

打开 [最新软件下载](https://github.com/Fish-Fy/WTmonitorTerminal/releases/latest)，下载最新版的 Windows x64 ZIP。

1. 将整个 ZIP 解压到可写文件夹，保留 `_internal` 和 `licenses` 文件夹。
2. 双击 `WTMonitorTerminal.exe`，无需安装 Python。
3. 点击右上方「设置昵称」，填写自己的游戏昵称；之后可随时「修改昵称」。
4. 游戏运行且本地 8111 接口有数据时，程序自动读取。没有实时数据时可使用已缓存地图进行规划。

适用于 Windows x64。请下载上述软件 ZIP；GitHub 自动生成的 **Source code (zip/tar.gz)** 只包含本下载仓库的说明文件。

## 主要功能

- 实时战术地图、轨迹、出生点和占领区显示。
- 地图缓存、地图库管理、人工命名和重复底图整理。
- 地图标点、连线和路线规划，支持离线编辑。
- 标点方案可绑定同图的多个任务，也可复制分享码。导入时先选地图，再预览点位与路线。
- 本局载具识别、敌友分组和击杀统计；本人并入「友方」，优先显示带战队前缀的名字。
- 可选高亮本局击毁过自己的玩家，持续到本局结束。
- 飞行仪表与两种姿态仪样式，包括飞机倾斜、地平线保持水平的样式。
- 手机浏览器只读显示地图和列表，同一局域网内使用，默认关闭。

## 界面语言

从 v1.1.1 起，桌面终端和手机页面支持简体中文与英语。打开「面板设置 → 界面语言」，选择 `English` 后重启终端。地图库支持用中英文搜索公开地图，击杀流会自动识别受支持的中英文战斗消息。

## 数据与使用范围

程序只读取本地战术接口，不注入、不读取游戏内存、不修改游戏文件。可显示的信息取决于游戏接口实际提供的数据。

下载包使用空白昵称与默认配置，内置 226 张地图的底图、缩略图及可用的公开任务布局。个人标点方案和对局记录均排除在下载包外。首次使用后，个人地图、方案和界面偏好保存在程序目录的 `data` 中，昵称保存在 `config.yaml` 中。

手机显示需在程序中手动开启，并使用面板列出的局域网地址。

## 常见问题

**打开后没有实时数据？**

确认游戏正在运行、本地 8111 接口可访问，并等待进入有数据的场景。敌友识别还需先设置本人游戏昵称。

**Windows 提示未知发布者？**

当前版本没有受信任的代码签名证书，下载或首次运行时可能出现系统提示。可将 ZIP 的 SHA-256 与 Release 提供的校验文件比对，确认下载文件一致；校验值不等同于安全认证。

**反馈问题时提供什么？**

请提供软件版本、复现步骤、预期结果和截图。分享配置或截图前，先检查是否包含自己的昵称、局域网地址或其他个人信息。

随包第三方组件许可保存在 `licenses` 目录，请保留该目录。

## English

WTMonitorTerminal is a Windows desktop tactical panel for War Thunder. It brings together map data, kill notifications, and flight instrument readings from the game's local interface at `127.0.0.1:8111`. You can place markers on the map and view the panel in a phone browser.

### Download and launch

Download the latest Windows x64 ZIP. [Latest release](https://github.com/Fish-Fy/WTmonitorTerminal/releases/latest)

1. Extract the entire ZIP to a writable folder. Keep the `_internal` and `licenses` folders.
2. Double-click `WTMonitorTerminal.exe`. The package runs without a separate Python installation.
3. Select **Set nickname** at the top right and enter your in-game nickname. You can change it later with **Edit nickname**.
4. While War Thunder is running, the terminal automatically reads any data available through the local interface on port 8111. You can use cached maps for planning when live data is unavailable.

WTMonitorTerminal supports Windows x64. Use the software ZIP attached to the release. GitHub's automatically generated **Source code (zip/tar.gz)** archives contain this download repository's documentation.

### Main features

- Live tactical map with movement trails, spawn points, and capture zones.
- Map caching and library management, including manual map naming and duplicate map cleanup.
- Map markers, links, and route planning, with offline editing.
- Bind a marker plan to multiple missions on the same map, or copy a share code. When importing, select the map first, then preview the markers and routes.
- In-match vehicle identification, friendly and enemy grouping, and kill counts. Your own entry appears under **Friendly**, and displayed names with squadron tags take priority.
- Optional highlighting for players who destroyed you. The highlight lasts until the end of the match.
- Flight instruments and two attitude indicator styles, including one in which the aircraft tilts while the horizon stays level.
- A read-only map and roster view in a phone browser on the same local network. This feature is off by default.

### Interface language

Starting with v1.1.1, the desktop terminal and phone view support Simplified Chinese and English. Open **Panel settings → Interface language** (**面板设置 → 界面语言** in Chinese), select **English**, and restart the terminal. You can search the map library for public maps by their Chinese or English names. The kill feed automatically recognizes supported combat messages in both languages.

### Data and usage

The terminal runs externally and reads game data only through the local tactical interface. It does not inject code into the game, read game memory, or modify game files. The information it can display depends on what the game exposes through that interface.

The download ships with a blank nickname and default settings. It includes 226 map images and thumbnails, plus available public mission layouts. Personal marker plans and match records are excluded from the package. After first use, the terminal stores personal maps, plans, and interface preferences in the program folder's `data` directory. It stores your nickname in `config.yaml`.

To use the phone view, enable it in the terminal and open the local network address shown in the panel.

### Frequently asked questions

**Why is there no live data after I open the terminal?**

Make sure War Thunder is running and the local interface on port 8111 is reachable. Then enter a game session that provides live data. Set your in-game nickname to enable friendly and enemy identification.

**Why does Windows show an unknown publisher warning?**

The current version uses no trusted code-signing certificate, so Windows may show a prompt during download or on first launch. Compare the ZIP's SHA-256 hash with the checksum file attached to the release to confirm that the files match. A matching checksum verifies the file's integrity. Code-signing trust is separate.

**What should I include in a bug report?**

Include your software version, steps to reproduce the issue, the expected result, and screenshots. Before sharing settings or screenshots, check for your in-game nickname, local network addresses, and any other personal information.

Keep the `licenses` directory, which contains the licenses for the third-party components shipped with the program.
