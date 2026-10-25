# v1.0.0 — 第一个能放心交给别人用的版本

两个在**自己电脑上跑**的听课工具，共用一套语音识别层。只监听 `127.0.0.1`，端侧模式下不联网。

## 下载

| 文件 | 大小 | 说明 |
|---|---|---|
| `tingke-suite-v1.0.0.zip` | 2.3 MB | **推荐**。两个应用 + 共用层，一份虚拟环境跑两个 |
| `buketang-v1.0.0.zip` | 1.5 MB | 只要「不听课」（讲义 + 录音 → 复习指导） |
| `mp3-transcriber-v1.0.0.zip` | 0.8 MB | 只要「语音转录工坊」（录音 → Markdown / Word） |

解压后双击 `start.bat`（macOS / Linux 用 `start.sh`）。首次启动会自建虚拟环境装依赖，之后秒开。

需要 Python 3.10+ 和 FFmpeg：

```bash
winget install Gyan.FFmpeg     # Windows
brew install ffmpeg            # macOS
sudo apt install ffmpeg        # Ubuntu
```

### 一个 Key 都不填能用到什么程度

| 场景 | 需要什么 |
|---|---|
| 录音转文字 | 本地 SenseVoice：点一次「下载模型」（230MB），之后纯 CPU、免费、离线 |
| 讲义 → 复习指导 | 一个 OpenAI 兼容的大模型 Key，或本机 Ollama |
| 只想看界面 | 「演示模式」，不联网、不花钱，产物结构真实 |

## 校验

```bash
sha256sum -c SHA256SUMS.txt     # macOS: shasum -a 256 -c
```

```
c80845295b1442b211acc3a724cda04ad34d2f916b184f2dc70ad7f49a11944b  tingke-suite-v1.0.0.zip
94a790c1c573ab886dffce004b97e49ce6b13d9b80ab6a25584a9deea93abbff  buketang-v1.0.0.zip
086576678df482e753faf1bb1792beefe566be37b5716011dd96b9484a04f70d  mp3-transcriber-v1.0.0.zip
```

## 这个版本有什么

### 不听课

- **课堂转录全文**：结果页上方折叠条，想核对老师原话就展开，带时间戳，和录音对得上
- **录音试听**：素材面板里就地展开播放器，支持拖动进度条
- **一键复制**：Markdown 原文直接粘进笔记软件；剪贴板被拒时给「手动复制」框
- **数据备份与恢复**：导出 zip 带走全部课程 / 课堂 / 录音 / 产物；恢复前自动另存当前数据，导错能捞回来；备份默认剥离 Key
- **考点搜索**：按「课堂名 + 主题 + 全部考点标题」匹配，忘了哪节课讲过什么也能搜到
- **删除课程**：只删分组，课下的课堂自动回到「未分类」，不会连数据一起删
- **深色模式**：跟随系统 / 浅色 / 深色，两个应用共用偏好

### 语音转录工坊

- **深色模式**、**一键复制全文**、**删除保护**（删任务会连产物一起删，先问一句）
- 预览弹层高度改为内容自适应

### 工程

- 两个应用统一到同一套纸感编辑风设计系统（`shared/web/theme.css`）
- 版本号单一来源 `shared/version.py`，页脚与 `/api/health` 都读它
- 启动脚本：`py -3` 优先 → 版本校验 → 建 venv → pip 失败自动换清华镜像 → 缺 FFmpeg 给提示
- 测试全会自己造数据、自己起服务、自己收场：`e2e_offline.py`、`ui_check.py`（98 + 46 项）、`selftest.py`、`asr_local.py`
- 发布链路也进了测试：`tools/package.py` 出包 → `tools/smoke_release.py` 解压、起服务、打接口、查换行符（59 项）→ GitHub Actions 上跑一遍
- 每个包都随附 `LICENSE`（MIT）与 `THIRD_PARTY_NOTICES.md`（含 PyMuPDF 的 AGPL 说明），冒烟里有断言防止漏带

完整改动见仓库里的 `CHANGELOG.md`；依赖与模型的许可清单见 `THIRD_PARTY_NOTICES.md`（注意 PyMuPDF 是 AGPL-3.0）。

## 已知限制

- **扫描版 PDF**（纯图片）提不出文字，只能截图，模型读不到内容
- **首次用端侧模型**要下 230MB（SenseVoice），国内走镜像
- **PPT 转 PDF** 在 Windows 上依赖本机 Office；macOS / Linux 需要 LibreOffice，或直接导入 PDF 讲义
- **界面验收脚本**需要 Chrome / Edge / Chromium 之一（仅开发时需要，普通使用不需要）
- 只支持 Windows / macOS / Linux 桌面端，没有手机版；窄窗口（≤680px）会退化成上下堆叠布局

## 反馈

装了跑不起来，请把启动窗口里的报错贴到 issue 里——启动脚本会保留完整信息，比截图有用。
