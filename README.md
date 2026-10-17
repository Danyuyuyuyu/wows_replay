# wowsreplay 解析插件

群里上传 `.wowsreplay` 文件后，插件自动调用 [wows-toolkit](https://github.com/landaire/wows-toolkit)
的 `minimap_renderer` 渲染小地图视频，并把 mp4 发送回群。

## 依赖

### 1. wows-toolkit 命令行工具

从 [Releases](https://github.com/landaire/wows-toolkit/releases) 下载工具包，解压到 `wows_toolkit/bin/`：
Windows 用 `wows_toolkit_tools_v<版本>_win64.zip`，Linux 见下文「在 Ubuntu / Linux 上部署」
（Linux 下文件名不带 `.exe`）。

| 文件 | 用途 |
| --- | --- |
| `minimap_renderer.exe` | 渲染小地图视频（GPU 编码） |
| `minimap_renderer_cpu.exe` | CPU 编码版本，GPU 不可用时替换使用 |
| `replayshark.exe` | 预检 replay 的游戏版本，不需要游戏数据 |
| `wows-data-mgr.exe` | 从游戏本体或 Steam 下载游戏数据 |

### 2. 游戏数据（--extracted-dir）

渲染需要与 replay 游戏版本匹配的数据。本机没有装游戏本体，因此数据取自
[wows-replay-data](https://github.com/landaire/wows-replay-data)，由脚本按需下载：

```powershell
# 查看上游有哪些版本（行首带 ` *` 的是本地已安装）
python zhenxun/plugins/wows_replay/tools/fetch_renderer_data.py --list

# 安装某个版本
python zhenxun/plugins/wows_replay/tools/fetch_renderer_data.py 15.8.0_13187581
```

默认生成 `<仓库根>/wows_replay_data/`，布局为：

```
wows_replay_data/
  common/<2位>/<18位>      内容寻址存储，渲染器实际读取的数据
  15.8.0_13187581/
    metadata.toml          路径 -> 内容哈希 的映射
    constants.json
    game_params.rkyv       真实文件（渲染器直接读取，不走 CAS）
    game_params.rkyv.zst
    translations/**.mo
```

> 渲染器是通过 `metadata.toml` 从 `common/` 取文件的，所以 `common/` 必须存在。
> 只复制版本目录、丢掉 `common/` 会出现
> `Failed to parse entity specs: alias.xml FileNotFound`。

脚本会在 `wows_replay_data_src/` 保留一个 blobless 的 git 缓存，
加装新版本时只下载该版本真正用到的对象。每个版本约占 400MB。

如果本机装了 World of Warships，也可以直接用官方命令生成：

```powershell
wows_toolkit/bin/wows-data-mgr.exe register --latest --path "C:/Games/World_of_Warships"
wows_toolkit/bin/wows-data-mgr.exe dump-renderer-data --latest -o ./wows_replay_data
```

游戏每更新一个大版本就要装一次对应数据，否则新 replay 会提示缺少数据。

## 配置

首次加载后可在真寻 WebUI 的插件配置里修改（模块名 `wows_replay`）：

> **改配置请走 WebUI，或直接改 `data/config.yaml`**。`data/configs/plugins2config.yaml`
> 是启动时按内存配置回写的产物，手改它会在下次启动被覆盖（简易配置优先级更高）。

> `TOOLKIT_DIR` / `DATA_DIR` / `DATA_SRC_DIR` 都支持两种写法：**相对路径**锚定在仓库根目录
> （不是进程工作目录，所以真寻从哪儿启动都一样），**绝对路径**原样使用。
> 默认值都是相对路径，因此整个仓库连同工具与数据一起搬走、换用户名或换系统都不用改配置。

| 配置项 | 默认值 | 说明 |
| --- | --- | --- |
| `ENABLE` | True | 是否启用群文件自动解析 |
| `TOOLKIT_DIR` | `wows_toolkit/bin` | 命令行工具目录（相对路径以仓库根目录为基准） |
| `DATA_DIR` | `wows_replay_data` | 游戏数据根目录（--extracted-dir，同上） |
| `MAX_FILE_SIZE_MB` | 200 | 单个 replay 大小上限 |
| `MAX_QUEUE` | 5 | 等待任务与下载预留名额的上限，满了直接拒绝 |
| `UPLOAD_QUIET_SECONDS` | 5 | 最后一次上传后等待多少秒汇总回复，正数，可填小数 |
| `UPLOAD_MAX_WAIT_SECONDS` | 20 | 从本批首次上传起最多等待多少秒汇总回复，正数，可填小数 |
| `RENDER_TIMEOUT` | 900 | 单次渲染超时（秒） |
| `RETENTION_HOURS` | 24 | 临时文件与产物保留时长 |
| `SHOW_PLAYER_NAMES` | False | 视频里是否显示玩家名 |
| `RENDERER_BINARY` | `minimap_renderer` | 渲染器文件名，Linux 无硬件编码时改成 `minimap_renderer_cpu` |
| `AUTO_CPU_FALLBACK` | True | GPU 编码失败时自动改用 CPU 编码重试一次 |
| `EXTRA_ARGS` | 空 | 追加给 minimap_renderer 的参数，如 `--show-trails --no-chat` |
| `ALLOWED_GROUPS` | 空 | 允许的群号，空为不限制 |
| `SEND_AS_BASE64` | False | 协议端与真寻不同机时改用 base64 发送 |
| `MAX_BASE64_MB` | 30 | base64 发送的大小上限，超过直接报错而不是硬发 |
| `DATA_SRC_DIR` | `wows_replay_data_src` | wows-replay-data 的 git 缓存，纯缓存可安全删除（同上） |
| `UPDATE_CHECK_ENABLE` | True | 是否定时检查上游更新 |
| `UPDATE_CHECK_INTERVAL_HOURS` | 24 | 检查间隔，默认每天一次 |
| `AUTO_INSTALL_DATA` | True | 缺数据时自动下载该版本再重试渲染 |
| `MAX_DATA_VERSIONS` | 5 | 保留的数据版本数，超出按版本号删最旧并回收内容存储 |
| `DATA_GRACE_DAYS` | 7 | 超出保留数量的旧版本，若在这么多天内重新下载过就先留着（天，0=不设宽限直接删） |
| `STAGE_TOOLKIT_UPDATES` | True | 新版工具下载到 `wows_toolkit/staging/` 并私聊超管 |
| `NOTIFY_SUPERUSERS` | True | 更新通知是否私聊超管 |

## 使用

- 群里上传 `.wowsreplay` 文件后立即下载并排队。同一机器人、群、上传者连续上传时，
  每次新上传都会重置 **5 秒静默计时**；最后一次上传后 5 秒统一回复一次，例如
  「收到 3 份回放，正在处理~」。连续上传最长 **20 秒**汇总一次，超过后归入下一批。
  两个时间分别由 `UPLOAD_QUIET_SECONDS` 和 `UPLOAD_MAX_WAIT_SECONDS` 配置；每批开始时读取，
  无效值回退默认值，最大等待时间小于静默时间时按最大等待时间汇总。
- 队列满、过大、重复及下载失败一起简短汇总。上传等待窗口不延迟下载和渲染。
- 成功时直接发送视频，不再单独发送「开始渲染」「命中缓存」「上传完成」或长文件名。
  视频先完成时不补过时确认；一批部分完成时只提示剩余待处理数量。
- 缺地图数据时仍给一句简短提示；下载或发送失败若发生在汇总之后，会补充失败原因。
- **每句话都先判断再说**，避免在手机上刷屏：数据没备齐时不会先说「正在画地图」再打脸，
  而是按实际情况给必要反馈。上游还没发布该版本时直接说明暂不可用，
  避免额外发送下载进度。

- 渲染是串行的（单 worker）+ 看门狗，任务循环意外退出会自动重启。
- 渲染先写临时 MP4，成功后原子发布；失败、超时或取消不会产生可命中的半成品缓存。
- 每五分钟和任务完成时清理过期文件，保护排队输入及正在交付的视频。
- 同一份 replay（内容 sha256 相同）在保留期内重复上传会**直接复用成品**，不再重新渲染
  （产物存在 `data/wows_replay/cache/<渲染配置签名>/<sha256>.mp4`，
  发送副本单独放在 `data/wows_replay/deliveries/<任务目录>/`，换文件名重传也能命中）。
  修改玩家名、附加参数或渲染器版本后使用新缓存；旧 `output/` 成品按原保留期清理，不复用。
- 不同群提交同一份回放且渲染设置相同时，共享渲染并分别交付视频；同一群对尚未完成任务的重复上传会在汇总中说明。
- 下载前先预留名额，防止并发上传突破队列限制；失败或取消自动释放名额。
- 启动时会做自检（工具、数据、编码器），结果写进日志，也可以用下面的命令查看。
- `@机器人 wowsreplay` 查看当前任务、处理阶段、耗时、下载和等待数量及自检。

## 自动检查更新

真寻会**每 24 小时检查一次**上游，发现新东西时**私聊超管**，不往群里发。

### 检查什么

| 对象 | 怎么判断 | 发现后 |
| --- | --- | --- |
| 游戏数据 | GitHub 上 `wows-replay-data` 有没有比本地更新的 build（API 限流时退回本地 git 缓存枚举目录名） | 通知超管 + 群里真有人传该版本 replay 时自动补 |
| wows-toolkit | `releases/latest` 的 tag 与 `wows_toolkit/.installed_version` 比对 | 下载解压到 `wows_toolkit/staging/<tag>/`，等人工切换 |

> 二进制**没有 `--version`**，版本号只能靠安装时写入的 `.installed_version` 记录；
> 没有 marker 时退回从下载的 zip 文件名推断。`setup_toolkit.sh` 会自动写这个文件。

### 按需自动补数据

群里有人传了本地没装的新版本 replay 时，插件先回一句「正在去下载（约 400MB）」，
拉完数据**继续渲染**，不用手动跑脚本。

- 同一个版本下载失败后进入 **6 小时冷却**，期间不再重复尝试；冷却到期会自动解除。
  另外每日更新检查一旦发现该版本已在上游出现，就**立刻解除冷却**——所以上游发布后
  既不用手动跑脚本，也不用重启真寻，直接再传一次 replay 就能补上。
- **上游还没发布**该版本时（游戏刚更新那几天最常见），会明确回一句「上游还没发布对应的
  地图数据」而不是让你去跑脚本——那种情况手动跑脚本同样找不到，只能等 landaire 发布。
- 装完自动裁剪：只保留最新 `MAX_DATA_VERSIONS` 个版本，并按各版本的 `metadata.toml`
  重算引用，回收 `common/` 里已经没人引用的内容对象（只删目录是省不出空间的）。
- 想关掉：`AUTO_INSTALL_DATA = False`。

### 为 wows_ship 提供客户端语言资源

`wows_ship` 的每小时维护会调用本插件的 `translations.sync_latest`；当本插件已加载且 `ENABLE`、`UPDATE_CHECK_ENABLE`、`AUTO_INSTALL_DATA` 均开启时，桥接按 `UPDATE_CHECK_INTERVAL_HOURS`（默认 24 小时）检查最新上游 build。已安装在 `DATA_DIR` 的完整渲染数据若包含所需 locale 与英文语言文件，就直接复用；否则在 `AUTO_INSTALL_DATA` 开启时，使用共用的 `DATA_SRC_DIR` sparse Git 缓存和下载锁，只物化两个 `global.mo` 文件，不下载完整渲染数据。资源缓存位于 `data/wows_replay/translations/<locale>/<build>/translations/`，其中 `translations.json` 记录 SHA-256 和大小，`data/wows_replay/translations/<locale>/sync.json` 记录检查状态。失败进入 6 小时冷却并保留先前有效资源。

桥接要求 `wows_replay` 插件已加载，且 `ENABLE`、`UPDATE_CHECK_ENABLE` 均开启；`AUTO_INSTALL_DATA` 关闭时只复用本地已有完整语言文件，不查询上游 build 列表也不下载新文件。资源来自第三方 [wows-replay-data](https://github.com/landaire/wows-replay-data)，不是 WG 官方 API 或游戏客户端下载。船库设置 `CLIENT_TRANSLATIONS_PATH` 时会直接采用指定的本地翻译根目录，跳过此桥接；船库仍会监视本地 `.mo` 和名称 JSON 的变化。桥接取得新文件后，船库巡检会自动发布包含新名称的 bundle；已开始的选船仍固定使用原 `data_version`。

真实上游资源 smoke（2026-10-11）：成功物化原始 `zh_sg` 5,557,150 B 与 `en` 5,941,831 B 的 MO 文件；原文件无 charset 声明，结构性 UTF-8 校验通过，SHA-256 manifest 校验结果为 `translations_ready=True`。随后服务器服务已重启加载桥接，状态为 ready；upstream_build/local_build 均为 15.9.0_13357625，`data/wows_replay/translations/zh_sg/sync.json` 已写入。本轮复用已有最新语言文件，没有下载新版本 MO。ship 插件 141 项、replay 插件 81 项测试全部通过（共 222 项）；两个插件及其 tests/tools 的 Ruff 检查通过，40 个相关文件格式检查通过，57 个 Python 文件通过 AST 检查。船库为 1017 艘、pending 0、图片失败 0；未进行真实 QQ 群消息触发验收。

### 工具更新为什么只下载不自动换

`game_params.rkyv` 是特定解析器版本序列化出来的，格式版本号就写在文件头的 `WUGP`
魔数后面：15.8.0 的数据是 `0x10`，15.9.0 变成了 `0x11`。旧渲染器读不了新格式，报
`game_params.rkyv ... is missing or incompatible`；新渲染器读旧格式则报
`the game params cache ... does not load`。

**重下数据不能换格式**：上游对每个 build 只保留它当年生成的那份，老 build 的数据
永远是老格式。正确做法是让新渲染器自己重建，在 `EXTRA_ARGS` 里加
`--recreate-game-params`：重建结果写到
`~/.local/share/wowstoolkit/build_overrides/<build>/game_params.rkyv`
（每个 build 约 35MB，只写一次、之后自动复用，**不动** `wows_replay_data/`）。

再加上 Windows 下运行中的 exe 覆盖不了、切换必须重启真寻，所以只下到 staging 并通知，
切换交给人工：停服 → 替换 `wows_toolkit/bin/` → 让 `--recreate-game-params` 常驻
`EXTRA_ARGS`（或对每个老版本各渲染一次）→ 重启。

启动自检会直接把这层信息写进日志/状态命令，不用等群里传 replay 才发现：

```
[自检] 游戏数据：4 个版本，最新 15.9.0_13357625
[自检] 数据格式：WUGP17（工具 v1.0.2 直接可读）      # 或 "需要 wows-toolkit ≥ v1.0.2，当前 v1.0.1 读不了"
```

（每个版本按 `WUGP` 魔数后的 u32 判定格式；工具版本与格式的对应表写在
`renderer.py` 的 `TOOLKIT_RKYV_FORMAT` 里，换工具时记得补。）

### 手动触发

- `@机器人 wowsreplay 检查更新`（仅超管）：立刻检查一次，结果回到当前会话。
- `@机器人 wowsreplay`：查看队列长度、渲染器、数据版本与编码器自检。

## 在 Ubuntu / Linux 上部署

插件代码本身跨平台，`TOOLKIT_DIR` / `DATA_DIR` 的默认值也一样，差别在二进制、运行库和性能。

### 1. 系统要求（实测 Linux 二进制）

release 里的 `wows_toolkit_tools_v<版本>_linux64.zip` 是 x86_64 动态链接 ELF：

| 二进制 | 最低 glibc | 额外依赖 |
| --- | --- | --- |
| `minimap_renderer` | 2.35（Ubuntu 22.04+） | `libvulkan.so.1`、`libstdc++6`、`libgcc-s1` |
| `replayshark` | 2.34 | `libgcc-s1` |
| `wows-data-mgr` | 2.39（Ubuntu 24.04+） | `libgcc-s1` |

**Ubuntu 20.04（glibc 2.31）跑不了 `minimap_renderer`**，需要 22.04 或更新。
`wows-data-mgr` 要求更高，但用不到它——数据交给本插件的脚本抓。

### 2. 装二进制

```bash
bash zhenxun/plugins/wows_replay/tools/setup_toolkit.sh
# 换版本：WOWS_TOOLKIT_VERSION=v1.0.2-beta2 bash .../setup_toolkit.sh
```

脚本会下载解压到 `wows_toolkit/bin/` 并 `chmod +x`。

### 3. 运行库：没有显卡也能跑

渲染走 wgpu/Vulkan。服务器没有 GPU 时装软件 Vulkan（lavapipe）：

```bash
sudo apt update
sudo apt install -y libvulkan1 libstdc++6 libgcc-s1 mesa-vulkan-drivers

wows_toolkit/bin/minimap_renderer --check-encoder
```

没有硬件编码时用 CPU 编码：把配置项 `RENDERER_BINARY` 改成 `minimap_renderer_cpu`，
或在 `EXTRA_ARGS` 里加 `--cpu`。（Linux 版 CLI 不需要 X11/Wayland，纯 headless 可跑。）

### 4. 游戏数据

和 Windows 完全一样：

```bash
python3 zhenxun/plugins/wows_replay/tools/fetch_renderer_data.py --list
python3 zhenxun/plugins/wows_replay/tools/fetch_renderer_data.py 15.8.0_13187581
```

Ubuntu 22.04 自带 Python 3.10，脚本需要 `tomllib`，装个 `tomli` 即可：

```bash
pip install tomli
```

### 5. 性能与回传

- 纯 CPU + 软件 Vulkan 会比独显慢很多（Windows 上 RX 6650 XT 一局约 21 秒）。
  建议先手工渲染一局量时间，再相应调大 `RENDER_TIMEOUT`，并把 `MAX_QUEUE` 调小。
- 视频回传用的 `file://`，要求协议端和真寻在同一台机器（或同一容器）上；
  不同机就把 `SEND_AS_BASE64` 打开。
- 跑 Docker 时把 `wows_toolkit/` 与 `wows_replay_data/` 挂进容器；
  要用 GPU 就 `--gpus all`（NVIDIA，需 nvidia-container-toolkit）或映射 `/dev/dri`（Intel/AMD）。

## 常见问题

| 现象 | 原因与处理 |
| --- | --- |
| 提示「缺少 x.y.z (build ...) 的游戏数据」 | 该版本的 replay 数据没装，会自动下载 |
| 提示「上游还没发布对应的地图数据」 | 游戏刚更新，[wows-replay-data](https://github.com/landaire/wows-replay-data) 还没发布该 build 的数据。**只能等**，手动跑脚本也一样找不到；上游发布后每日检查会自动解除失败冷却，届时再传一次即可 |
| 提示 replay 文件下载失败 | 协议端与真寻不在同一台机器（群文件给的是协议端本机路径，真寻取不到），或群文件直链这会儿不可达。插件会重新取地址重试两次。注意 `SEND_AS_BASE64` 只影响**视频回传**，跟下载 replay 无关 |
| 报 `下载 replay 失败（<主机>）：All connection attempts failed <- ...` | 主机能解析但连不上。插件会自动换一个下载地址重试两次（见下），三次都失败才报错。括号里是**最后一次用的主机**；如果反复出现，看日志里前两次的 `换一个下载地址重试` 行，能对比出到底是哪个节点不可达 |
| 渲染超时 / 失败 | 查看日志里 minimap_renderer 的输出，可调大 `RENDER_TIMEOUT`，或在 `EXTRA_ARGS` 里加 `--cpu` |
| 视频发不出去 | 打开 `SEND_AS_BASE64` |
| 提示「视频有 xx MB，超过 base64 发送上限」 | 大视频不适合走 base64，让协议端与真寻部署在同一台机器上用 `file://` 发送 |
| 提示「replay 实际上有 xx MB」 | 对端声明的大小不可信，插件按真实下载量拦截，调大 `MAX_FILE_SIZE_MB` 或换小文件 |

## 实现说明

| 文件 | 作用 |
| --- | --- |
| `__init__.py` | 插件元数据、群文件通知入口、状态命令、启动队列 |
| `config.py` | 配置项与路径 |
| `downloader.py` | 从群文件上传通知取下载地址并落盘（支持 `file://` 与 http）；流式落盘、边下边算 sha256 并强制校验真实大小；连接失败会**重新取地址**重试两次 |
| `messages.py` | 对外文案与简短批量汇总 |
| `replies.py` | 按群和上传者合并确认，协调计时、结果交付与关停 |
| `renderer.py` | 版本探测、渲染（GPU 失败自动降级 CPU）、`--check-encoder` 自检 |
| `worker.py` | 串行队列 + 看门狗、渲染结果缓存、消息回报、过期清理 |
| `updater.py` | 更新检查、按需补游戏数据、版本裁剪与内容存储回收、工具 staging |
| `translations.py` | 为 wows_ship 按需同步并校验客户端语言文件 |
| `tools/fetch_renderer_data.py` | 从 wows-replay-data 取完整渲染数据，或稀疏物化指定语言文件 |
| `tools/setup_toolkit.sh` | Linux 上下载解压 wows-toolkit CLI |


## 修复回归测试

无需启动机器人、联网或加载真实配置，可运行：

```bash
python -m unittest discover -s tests/plugins/wows_replay -v
```

测试覆盖并发准入、跨群订阅、配置缓存失效、原子渲染、CPU 降级、安装中断、
数据完整性、旧版本保护及下载重试。真实 GPU 编码和 OneBot 视频交付仍需运行环境验证。

合并回复还覆盖 5 秒计时重置、20 秒上限、多用户隔离、批量拒绝、迟到结果、
视频先完成与关停清理；计时测试使用虚拟时钟，无需实际等待。
