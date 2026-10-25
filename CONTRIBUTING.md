# 参与开发

欢迎提 issue 和 PR。这个仓库只有一个人维护，所以把能自动化的都自动化了——**提交前跑一遍自检，比在 PR 里解释半天有用**。

## 环境

```bash
# 先 fork，再 clone 你自己的 fork（仓库页 Code → HTTPS 复制地址）
git clone <你复制的地址> tingke && cd tingke
python -m venv .venv

# Windows
.venv\Scripts\activate
# macOS / Linux
source .venv/bin/activate

pip install -r requirements.txt
pip install websocket-client        # 只有跑界面验收才需要
```

FFmpeg 是硬依赖（音频转码与切片）：

```bash
winget install Gyan.FFmpeg     # Windows
brew install ffmpeg            # macOS
sudo apt install ffmpeg        # Ubuntu
```

## 跑一遍

```bash
cd buketang
python tests/test_integrity.py
python tests/e2e_offline.py
python tests/ui_check.py
cd ../mp3-transcriber
python tests/selftest.py
python tests/ui_check.py
```

这些脚本都不花钱、不需要任何 Key，也不会碰你 `data/` 里的真实数据（自检用临时目录）。

界面验收需要 Chrome / Edge / Chromium 之一；装在非常规位置可以用 `CHROME_PATH` 指定。

### CI 会替你跑什么

推上去之后 GitHub Actions 跑完整性回归 `test_integrity.py`、`selftest.py` + `e2e_offline.py`（真转音频、真把 PPTX 转 PDF 再截图），以及打包 → 解压 → 起服务 → 打接口的发布包冒烟。

`ui_check.py` 不在里面——它要真实浏览器的 CDP，CI 机器上没有。所以**前端改动请本地跑一遍再提 PR**，这是唯一靠自觉的一环。

## 代码约定

### 前端

- **没有构建步骤**。原生 JS + ES 模块，改完刷新即可。别引 React / Tailwind，这是本地小工具，不值得多一层工具链
- 通用控件（按钮、表单、弹层、抽屉、Toast）都在 `shared/web/theme.css`，应用自己的 `style.css` 只写独有版式。加新控件先想想是不是两个应用都要用
- 颜色只用 CSS 变量（`--paper` / `--ink` / `--accent` …），不要写死色值——否则深色模式会有一个角落忘改
- 用户输入渲染进 DOM 前一律过 `esc()`

### 后端

- Flask 单文件路由（`app.py`）+ `core/` 分模块，不做过度抽象
- 服务只监听 `127.0.0.1`
- 长任务走线程 + 轮询状态，不引 Celery / Redis
- 任何用户数据落到 `data/` 下，路径通过 `config.DATA_DIR` 取，别硬编码

### 共用层

- 改 `shared/asr` 要同时验两个应用：`buketang/tests/asr_local.py` 和 `mp3-transcriber/tests/selftest.py`
- 改 `shared/web/theme.css` 要同时看两个应用（尤其深色模式）
- 版本号只在 `shared/version.py` 改，别在别处写死

## 提交信息

大致遵循 `feat: / fix: / docs: / refactor: / test: / chore:`，一句话说清「改了什么、为什么」。

## PR 检查清单

- [ ] 相关自检脚本全绿
- [ ] 改了 UI：两个应用的深浅色都看过，窄屏（≤820px）不横向溢出
- [ ] 新增对外接口/环境变量：README 与 CHANGELOG 都补了
- [ ] 没有把 `data/`、`models/`、`.venv/` 或任何密钥提交进来

## 许可

贡献的代码按 [MIT](LICENSE) 授权。
