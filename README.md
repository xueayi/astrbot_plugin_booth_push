# AstrBot Booth 每日上新推送

直接爬取 Booth.pm，不依赖 booth Lens 或其他外部取数服务。插件每天抓取 `3D衣装` 和 `3D髪型` 的新上架商品，生成两列长图和纯文本清单后推送到 AstrBot 会话。

![Booth Push](logo.png)

## 功能

- 本地增量抓取：按 `sort=new` 浏览新上架商品，再读取商品详情 JSON。
- 时间窗口：只推送过去 24 小时发布且未推送过的商品。
- KV 去重：推送成功后把商品 ID 写入 AstrBot 插件 KV，重启不重复推送。
- 免费/付费分组：价格 0 或标题含免费标记视为免费。
- LLM 翻译：批量翻译标题，失败时保留日文原标题。
- 长图渲染：两列商品卡片、中文字体自动探测、emoji 清理。

## 安装

把整个 `astrbot_plugin_booth_push` 目录放到 AstrBot 的 `data/plugins/` 下，然后启动或重载插件。

依赖：

```text
httpx
pillow
```

## 配置

WebUI 插件配置项：

- `daily_cron`：每日爬取并推送时间，默认 `0 8 * * *`。
- `timezone`：Cron 时区，默认 `Asia/Shanghai`。
- `startup_update`：插件加载后自动进行一次初次抓取，默认开启。
- `category_quota`：两个类目免费/付费各自的推送数量。
- `crawl_pages` / `crawl_workers` / `crawl_delay`：爬取页数、并发和请求间隔。
- `http_proxy`：可选代理地址。
- `llm_provider` / `enable_translation`：标题翻译模型。留空使用 AstrBot 当前默认对话模型。
- `push_targets`：目标 `unified_msg_origin`；也可以在会话中使用 `/booth bind`。
- `font_path`：中文字体路径，留空自动探测或下载 Noto Sans SC。

## 指令

- `/booth bind`：绑定当前会话。
- `/booth unbind`：解除当前会话绑定。
- `/booth update`：只执行一次爬取，不推送。
- `/booth push`：先爬取，再推送当前新商品。
- `/booth status`：查看定时、配额、爬取和推送状态。
- `/booth check`：测试翻译模型通路。
- WebUI：插件管理页提供“手动拉取更新”“手动抓取并推送”按钮，并动态显示/增删 `/booth bind` 生成的订阅。

修改 `daily_cron` 后需要重载插件才会重新注册定时任务。

## 手动填写 UMO

配置项 `push_targets` 的每项格式为 `platform_id:message_type:session_id`，例如：

```text
飘雪:GroupMessage:1063905732
```

如果使用 QQ OneBot，`platform_id` 通常是 WebUI 中平台适配器的名称，`message_type` 为 `GroupMessage` 或 `FriendMessage`，`session_id` 是群号或 QQ 号。也可以用 `/booth bind` 在目标会话里直接绑定。

## 数据位置

- KV：`last_push_at`、`last_update_at`、`seen_ids`、`translations`。
- 缩略图：`data/plugin_data/astrbot_plugin_booth_push/cache/thumbs/`。
- 长图：`data/plugin_data/astrbot_plugin_booth_push/images/daily.png`。

## 注意

这是针对 Booth 公开页面的增量抓取，请保持合理的请求间隔和并发数，并遵守 booth.pm 的服务条款。
