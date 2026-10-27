# 媒体服务器 (Emby / Jellyfin)

> 客户端在 `src/amane/mediaserver/`, 任务在 `src/amane/handlers/media_server.py`. 本文记录与服务器交互的契约与取舍; 配置项在 `src/amane/config/manager.py::MediaServerConfig`.

## 定位

单台服务器, 只承载人物: `ACTOR_IMPORT` 从服务器拉人物建本地档案, `ACTOR_SYNC` 把本地人物资料写回服务器. 影片元数据仍以 DB 为真值、NFO 为派生, 不反向. 两个任务都经 `build_handlers` 构造, 配置变更随 worker 重建生效.

## 请求形状

- 根路径按 `server_type` 区分: Emby 加 `/emby` 前缀, Jellyfin 用站点根; 用户只配置 host 与端口.
- 鉴权用 `X-Emby-Token`, 两种服务器都接受; 出站一律经 `WebClient`, 超时取 `media_server.timeout`, 不走刮削限速.
- 人物列表走 `/Persons`, 按本次实际返回条数推进 `StartIndex`, 直到 `TotalRecordCount`; 服务器自行截断单页条数时不会漏页.

## 回写契约

- 更新以人物列表返回的原始 DTO 为底稿整体回传 (`POST /Items/{id}`), 只覆盖本次要写的字段: 服务端其它字段与原图标记不得丢.
- `overview_mode=append_original` 把服务器原简介当作「经历」追加在本地简介之后; 服务器简介已以本地简介开头时原样保留, 重复回写不叠加.
- Tags 与 ProviderIds 取并集 (服务器现有值在前, 本地覆盖同名键); 结果与服务器现值相同时不发请求.
- 头像取 `image_urls[0]` 的 Resource 本地文件, 缺文件时跳过, 不阻断其余字段.
- 写完调用 `/Items/{id}/Refresh` 且 `MetadataRefreshMode=None`, 避免提供方把刚写的字段覆盖回去.
- 单个演员失败只计数, 不中止整批.

## 导入契约

- 展示名或别名命中即视为本地已存在, 不覆盖; `import_missing_actors` 关闭时任务直接失败, 不空跑.
- 新建行只写服务器给出的字段, `field_sources` 记 `emby` / `jellyfin`; 服务器没有的性别与头像不猜.
