# Sidekick Desktop（对齐 Cursor 实时内嵌预览）

桌面壳用 Electron **BrowserView** 把真实网页叠在侧栏「浏览器」区域上——可点击、滚动、HMR，而不是截图轮询。

## 启动

仓库根目录双击或运行：

```powershell
.\start-desktop.bat
```

脚本会自动安装 Python 依赖、Playwright Chromium（如缺）、desktop/ui 的 npm 包、构建 UI，再启动 Electron。

可选环境变量：

- `SIDEKICK_PYTHON` — Python 解释器（也可用 `.sidekick-python`）
- `ELECTRON_MIRROR` / `PLAYWRIGHT_DOWNLOAD_HOST` — 国内镜像（脚本已带默认 npmmirror）

## 用法

1. 侧栏 **浏览器**，或对话里 Ctrl+点击链接 →「在沙盒打开」
2. 地址优先用 `http://localhost:5173`（Windows 上 Vite 常只监听 IPv6）
3. **选择元素** → 点选 → 附件进对话

角标「实时」= 桌面 live；纯网页版为「截图」退化模式。

## 说明

- 后端：桌面壳自动 `python main.py serve`（8787 已有服务则复用）
- 给人看的预览走 BrowserView；Agent `browser_*` 仍可用 Playwright
