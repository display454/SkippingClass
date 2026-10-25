# 第三方组件与许可

本仓库**自己的代码**是 [MIT](LICENSE)。

下面这些组件不随仓库分发：Python 包在首次启动时由 `pip` 从 PyPI 现装，语音模型在你点「下载模型」时从上游现下。所以这里只是一份清单，方便你判断自己的使用场景需要遵守什么。

## 运行期依赖

| 组件 | 版本要求 | 许可 | 用在哪 |
|---|---|---|---|
| Flask / Werkzeug | ≥3.0 | BSD-3-Clause | 本地 HTTP 服务 |
| requests | ≥2.31 | Apache-2.0 | 调用云端语音识别与大模型 |
| python-docx | ≥1.1 | MIT | 生成 Word 产物 |
| python-pptx | ≥1.0 | MIT | 直读 `.pptx` 文字（没装 Office 时的兜底） |
| PyMuPDF | ≥1.24 | **AGPL-3.0** 或 Artifex 商业授权 | 讲义转 PDF、按页渲染截图 |
| sherpa-onnx | ≥2.0 | Apache-2.0 | 端侧 SenseVoice / VAD 推理 |
| NumPy | ≥1.24 | BSD-3-Clause | 端侧音频采样数据处理 |
| psutil | ≥5.9 | BSD-3-Clause | 按本机内存建议本地模型的尺寸 |
| pywin32 | ≥306（仅 Windows） | PSF | 调本机 Office 把 PPT / Word 转 PDF |
| faster-whisper | ≥1.0 | MIT | 可选的另一个端侧引擎（默认不装） |

### PyMuPDF 是 AGPL，这条要看清楚

`不听课` 能把讲义里的每一页截出来贴在知识点旁边，靠的是 PyMuPDF。它是 **AGPL-3.0 / 商业授权双轨**，不是宽松许可。

- **自己用**：没有任何额外义务，装了就装
- **原样转发本仓库**：仓库里没有 PyMuPDF 的代码，也没有把它打包进去，只是写了 `requirements.txt` 里的一行
- **把打包好的可运行版本再分发**（放到自己服务器上给别人下、批量拷进 U 盘、做成一键安装包）：AGPL 要求你把对应的完整源码一并提供出来。想避开这条，可以去 Artifex 买商业授权，或者把 `core/ingest.py` 里的 PDF 渲染换成许可更宽松的实现（比如 pypdfium2，BSD-3）

## 语音模型（不随仓库分发）

| 模型 | 下载来源 | 许可 |
|---|---|---|
| SenseVoice（sherpa-onnx 打包版，int8） | `github.com/k2-fsa/sherpa-onnx` 的 releases | 解压后的模型目录里带 `LICENSE`，指向上游 [FunASR](https://github.com/modelscope/FunASR) 仓库的许可条款 |
| `silero_vad.onnx` | 同上 | 以上游 `snakers4/silero-vad` 的条款为准 |

两者都由用户在本机下载、只在本机使用，本仓库不转存、不再分发。商用前请自行确认上游条款。

## 前端

没有任何第三方库：没有 React / Vue / jQuery / Tailwind，没有 CDN 引用，没有构建步骤。

字体用的是系统已有的字体栈（`-apple-system` / `Segoe UI` / `Microsoft YaHei UI` / `Noto Serif CJK SC` 等），**没有打包任何 webfont**，因此不涉及字体授权。

## 示例与截图

- `*/assets/screenshots/*.png`：本项目自己截的界面图，随仓库以 MIT 提供
- `mp3-transcriber/examples/*`：用「演示模式」生成的示例产物，内容为虚构文本

## 仓库里没有的东西

以下都在 `.gitignore` 里，不会出现在仓库或源码包里：

- `data/`：你导入的课堂、录音、讲义、生成结果，以及 API Key
- `models/`：约 230MB 的端侧模型
- `dist/`：发布包 `zip`（走 GitHub Releases 分发）
- `restore_backups/`：恢复备份时自动另存的旧数据
