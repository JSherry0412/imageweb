# 本地生图工作台

一个运行在本机的图片生成网页，可连接 OpenAI 兼容的图片服务。支持文生图、参考图编辑、Markdown/Base64 图片自动还原、图片放大预览和下载。

## 启动

1. 安装 Python 3.10 或更高版本。
2. 双击 `启动图片工作台.bat`，等待依赖安装并启动。
3. 浏览器打开 `http://127.0.0.1:7860`。

也可以在终端运行：

```powershell
python -m pip install -r requirements.txt
python app.py
```

首次使用时，在左侧配置 API 地址、密钥和模型。连接设置支持多套命名方案，可按网站预先保存后快速切换；原有设置会自动迁移为“默认方案”。方案、API 密钥、生成历史和图片保存在项目目录的 `data/` 中，关闭 BAT 只会停止本地服务，不会删除这些文件。不要删除 `data/` 文件夹。API 密钥不会发送到网页脚本或写入图片记录。

## 接口模式

- **图片生成接口**：默认使用 `/images/generations`；上传参考图时使用 `/images/edits`。
- **聊天补全接口**：使用 `/chat/completions`，解析文本回复中的 Markdown 图片/Base64 数据。参考图会作为 data URL 随消息发送。
- 可编辑 API 路径，以适配路径不同的兼容服务。
- “将 Markdown/Base64 响应转成图片”开启后，自动提取 `![...](data:image/...;base64,...)`、裸 data URL 或纯 Base64 图片。标准图片 API 的 `data[].b64_json` 与直接图片响应始终自动处理。

每次生成最多处理一张图片。具体尺寸、参考图编辑和 response_format 支持情况取决于服务商。

## 图片保留

生成图片保存在 `data/images/`。未下载图片在生成 72 小时后自动删除；下载过的图片会保留。图片卡片菜单可手动删除任意图片。

服务默认只绑定 `127.0.0.1`。不要把服务改为公开监听，也不要提交 `data/settings.json`、`data/profiles.json` 或个人生成图片。


