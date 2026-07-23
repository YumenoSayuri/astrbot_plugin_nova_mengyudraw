# CloudComfyUI 插件

## 简介

CloudComfyUI 插件默认通过 Node undici 桥接调用云端绘图接口，并优先使用同一条 Node 出网链路下载返回图片，适合云服务器无法直接访问返回图地址的场景。

支持能力：

- AI 主动文生图
- AI 主动改图
- `/comfyui` 手动命令生图/改图
- 命令开始时先发送“正在使用comfyui画图，请稍后...”
- 自动从消息、引用消息中提取图片 URL
- 传入 `--image_source` 时自动作为编辑原图
- 命令检测到“有图上下文”时自动切换默认编辑模型
- 改图未指定分辨率时可按面板开关分别自动缩小超限原图或自动放大小图
- 编辑结果发送前可自动转 PNG，默认开启，兼容旧版 PC QQ
- 支持强制全部图片合并转发，或通过 Sightengine 智能识别漏点图后仅折叠敏感结果
- 支持多组 Sightengine 凭据轮询，审核重试时自动切换下一组
- 转发摘要仅显示清洗后的模型名与真实成图尺寸
- 用户可见的 HTTP 错误仅显示状态码，详细内容保留在 AstrBot 日志
- prompt 内解析并删除 `modelX`
- prompt 内解析并删除 `cfgX`
- prompt 内解析并删除 `stepsX`
- prompt 内解析并删除 `seedX`
- prompt 末尾解析分辨率
- 本地生成随机 seed
- 命令链路与 AI 主动链路统一参数构造
- 命令命中后显式 `stop_event`，避免失败后继续唤起主 AI
- 命令成功时默认只发图，成功摘要写日志
- 记录真实成图尺寸，避免请求尺寸与实际输出不一致时误报
- Cloudflare Worker 中转兼容

---

## v1.5.0 更新说明

### 智能漏点图片合并转发

面板新增：

- `smart_forward_sensitive_images`：开启 Sightengine 智能审核
- `sightengine_credentials`：多组凭据列表，每行格式 `api_user:api_secret`
- `moderation_threshold`：核心裸露阈值，默认 `0.8`
- `moderation_timeout`：单次审核超时，默认 `10` 秒
- `moderation_retries`：审核失败重试次数，默认 `2`

插件使用 Sightengine `nudity-2.1` 本地文件上传审核，判定分数取 `sexual_activity`、`sexual_display`、`erotica` 三项最大值。低胸、内衣等仅命中 `suggestive` 的图片不会单独触发合并转发。审核重试全部失败时安全兜底为合并转发。

多组凭据会按请求轮询，重试时继续切换下一组。示例：

```text
1044950338:此处填写对应的 api_secret
另一组 api_user:另一组 api_secret
```

`send_image_as_forward=true` 时会强制所有图片合并转发，优先级高于智能审核。

### 精简结果与错误提示

合并转发摘要和 LLM 工具成功返回统一为：

```text
绘图结果
模型: Wainsfw illustrious v16
尺寸: 832x1216
```

模型名前置的 `[全新模型]`、`[新服开放]` 等方括号标签会自动移除，不再显示积分与 Seed。HTTP 错误对用户仅保留状态码，例如：

```text
生成图片失败: 504
```

完整错误仍写入 AstrBot 日志用于排障。

---

## v1.4.11 更新说明

### 新增请求后端切换 (Python 直连 / Node 桥接)

- 新增面板配置 `request_backend`，默认 `python`。
- Python 直连已全面伪装 Chrome UA 绕过 CF 拦截。
- 支持在 Python 和 Node 桥接间自由切换，且生图与下载均会自动互相兜底。

---

## v1.4.10 更新说明

### 新增默认编辑模型索引面板项

本次新增新的面板配置项：

- `default_edit_model_index`

默认值：

- 默认编辑模型索引：`14`

行为说明：

- 图片编辑时默认读取 `default_edit_model_index`
- AI 主动改图和命令检测到图片上下文时，都会优先使用该配置
- 如果你在面板里改成别的模型索引，编辑流程会直接跟着变，不再死写 `14`

---

### 旧版新增 edit 结果转 PNG 与强制合并转发开关

本次新增两个新的面板配置项：

- `convert_edit_result_to_png`
- `send_image_as_forward`

默认值：

- 编辑结果发送前自动转 PNG：`true`
- 强制全部图片合并转发：`false`

行为说明：

1. 当使用编辑模型生成结果时：
   - 如果 `convert_edit_result_to_png=true`
   - 会在发送前优先将编辑结果转换为 `PNG`
   - 用于提升旧版 PC QQ 对改图结果的兼容性

2. 当旧版强制开关 `send_image_as_forward=true` 时：
   - 图片结果不会直接裸图发送
   - 而是以合并转发节点方式发出
   - 节点中的图片内容使用 base64 图像数据构造，避免宿主机 Windows 路径与 NapCat 运行环境不一致时的跨系统文件访问问题
   - 可用于避免群里直接露出敏感图片

---

## v1.4.6 更新说明

### 自动缩小 / 自动放大 拆分为两个独立面板开关

现在编辑模型的自动分辨率策略已经拆分为两个独立配置项：

- `auto_shrink_edit_resolution`
- `auto_enlarge_edit_resolution`

默认值：

- 自动缩小超限原图：`true`
- 自动放大小图：`false`

行为说明：

- 当编辑模型且未手动指定分辨率时：
  - 如果原图单边超过 `1980`，并且 `auto_shrink_edit_resolution=true`，则自动等比缩小
  - 如果原图单边小于 `1980`，并且 `auto_enlarge_edit_resolution=true`，则自动等比放大
- 如果对应开关关闭，则不执行对应方向的缩放；在改图未手动指定分辨率时，直接使用原图分辨率

---

## v1.4.5 更新说明

### 改图自动按原图比例缩放到单边不超过 1980

现在当编辑模型场景下：

- 用户没有显式写分辨率
- 但消息里带图、引用了图，或传入了 `--image_source`

插件会先读取原图真实尺寸，再按原图宽高比自动缩放，请求尺寸自动控制在单边不超过 `1980` 的范围内。

当前策略为：

1. 保持原图宽高比不变
2. 如果原图较小，则等比放大到最长边尽量接近 `1980`
3. 如果原图过大，则等比缩小到最长边不超过 `1980`
4. 在不超过 `1980` 的前提下，优先选择比例误差更小的整数尺寸组合

例如：

- `1124x793` 会自动缩放到 `1980x1397`
- `3000x2000` 会自动缩放到 `1980x1320`
- `900x1400` 会自动缩放到 `1273x1980`

如果用户自己在 prompt 里明确写了分辨率，例如 `1216x832`，则仍然以用户指定为准，不会被自动覆盖。

---

## v1.4.1 修复说明

### 编辑模型尺寸误报修复

已修复编辑模型场景下，日志显示请求尺寸例如 `832x1216`，但实际下载到的图片却是 `512x512` 时，插件仍误把请求尺寸当成成图尺寸的问题。

现在修复为：

- 编辑模型请求体也会显式携带 `width` 与 `height`
- 下载图片后，插件会读取真实图片像素尺寸
- 返回结果中的 `width` / `height` 优先使用真实成图尺寸
- 同时保留 `requested_width` / `requested_height` 作为请求参数记录

---

## v1.4.0 更新说明

### 新模型索引表已同步

当前插件已切换到新的模型索引体系：

```text
0  = Miaomiao Harem vPred Dogma 1.1
1  = MiaoMiao Pixel 像素 1.0
2  = NoobAIXL V1.1
3  = illustrious_pencil 融合
4  = [全新模型]one_obsession
5  = [全新模型]MiaoMiao RealSkin EPS 1.3
6  = [全新模型]Newbie exp 0.1
7  = [全新模型]MiaoMiao RealSkin vPred 1.1
8  = [新服开放]MiaoMiao RealSkin vPred 1.0
9  = [全新模型]Wainsfw illustrious v17
10 = [全新模型]Wainsfw illustrious v16
11 = [新服开放]Wainsfw illustrious v15
12 = [新服开放]MiaoMiao Harem 1.75
13 = [新服开放]MiaoMiao Harem 1.6G
14 = Qwen Image Edit2511版
```

### 默认编辑模型可在面板修改

默认编辑模型索引由面板里的 `default_edit_model_index` 控制，默认值为 `14`。

因此：

- 不再需要手动在编辑 prompt 里写 `model14`
- AI 主动改图会读取面板里的 `default_edit_model_index`
- 命令检测到图片时，也会自动切换到该默认编辑模型

---

## 命令说明

### 基础命令

```text
/comfyui <提示词>
```

### 文生图示例

```text
/comfyui 1girl, silver hair, red eyes, portrait
/comfyui Nahida, Genshin Impact, forest background, model10, cfg3.5, steps28, 1216x832
```

### 自动改图示例

```text
/comfyui 把这张图头发改成黑发
/comfyui 把这张图背景改成海边 --image_source=https://example.com/a.png
```

### 图片编辑说明

如果当前消息、引用消息中带图，或显式提供了 `--image_source`，插件会自动提取该图 URL，并默认切换到面板配置的默认编辑模型索引。若没有手动指定分辨率，则会根据面板中的 `auto_shrink_edit_resolution` 与 `auto_enlarge_edit_resolution` 两个开关，分别决定是否自动缩小超限原图或自动放大小图。

---

## 面板默认值

这些值主要由插件面板配置控制：

- 默认文生图模型：`10`
- 默认编辑模型：`default_edit_model_index`（默认 `14`）
- 默认步数：`28`
- 默认 CFG：`7`
- 编辑模型自动缩小超限原图：`true`
- 编辑模型自动放大小图：`false`
- 编辑结果发送前自动转 PNG：`true`
- 强制全部图片合并转发：`false`
- 智能合并转发漏点图片：`false`
- Sightengine 核心裸露阈值：`0.8`
- Sightengine 审核超时：`10` 秒
- Sightengine 审核失败重试：`2` 次
- 默认 negative_prompt：使用面板配置
- 默认代理：按面板配置
- 默认 base_url：`https://sd.exacg.cc`

---

## 版本

当前版本：`v1.5.0`