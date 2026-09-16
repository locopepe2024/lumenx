# 独立视频复刻功能 v1

日期：2026-09-17。状态：已启动规划，尚未实现。分支：feature/lumenx-recreation-v1。
依赖：PR #1 已合入 feature/lumenx-multi-user-v1，合并提交 44ee54d。

## Observed

- PR #1 提供 Studio H3/Seedance 模型保留、参考素材提交、provider ID 持久化与恢复、本地素材归属检查；198 项 mocked 测试复跑通过。
- 生产源码目录 /srv/lumenx/repo；Docker 容器 lumenx-backend，工作目录 /app；持久化挂载 /srv/lumenx/output → /app/output。
- 2026-09-17 核验生产仍是 eb9d44c，FFmpeg 和 FFprobe 均为 7.1.5。二进制存在不代表已有复刻分析 API 或 media 主机集成。
- 桌面“复刻”样本为15秒、变帧时间戳；已核实切点4.016667、9.083333、10.400000秒。联系表采样时间不能替代切点。
- 独立 GPT 5.6 Sol + H3 1.6.0 请求在提供明确时间线后保留四镜及静音；尚未验证H3接受和成片质量。

## 目标与边界

新增独立“复刻”入口及复刻项目记录，复用现有身份、资产存储、模型适配器及生成任务；不把自由Chat历史当作时间线事实来源，不复制一套provider提交逻辑。
第一版支持原片结构复刻与产品外观替换。精确切点是分析/装配约束；单次生成不能承诺帧级遵守原片。保留原声、静音和新声音设计作为明确不同选择。
暂不做自动ASR、口型替换、掩膜追踪或原片逐像素无损编辑。media主机接入方式未确认，不能假定Chat能直接远程执行FFmpeg。

## 资产管理与检索前置设计

### 现有资产库的能力边界

现有 `GlobalAssetLibrary` 是角色、场景、道具三类 JSON 资产池，文件为
`output/library_assets.json`。前端资产库在加载所有系列、独立项目和全局池后，
只在浏览器内按名称/描述做线性过滤；没有服务端分页、标签、媒体类型、来源镜头、
时间范围、指纹或派生关系检索。上传接口只登记图片路径，不能登记原片视频、镜头或
证据帧。资产删除的引用检查也只覆盖 storyboard 的角色、场景和道具字段。

因此复刻切片不能把每个镜头和证据帧直接作为 `Character`、`Scene` 或 `Prop`；这会
污染现有资产池，也无法可靠地从几十个镜头中检索同一原片、同一时间范围或同一替换
对象。现有 UI 的检索实现是客户端事实，不代表对生产大库有可接受的性能。

### 统一媒体索引（下一切片必须先做）

引入 owner-scoped `MediaRecord` 与 `AssetEntry` 两个概念，并让复刻和现有资产库
通过 `media_id` 关联：

| 记录 | 用途 | 必要检索字段 |
| --- | --- | --- |
| `MediaRecord` | 一份不可变文件及其派生关系 | `media_id`, owner, kind, sha256, storage key, mime, bytes, duration, width, height, parent_media_id |
| `AssetEntry` | 用户可见的可复用对象 | `asset_id`, media_id, display name, asset kind, tags, source project, starred, created_at |
| `ShotMediaBinding` | 原片与镜头/证据的关系 | source media, analysis revision, shot id, start/end PTS, frame PTS, role |

`kind` 至少区分 `source_video`, `shot_clip`, `evidence_frame`, `contact_sheet`,
`reference_image`, `generated_video`, `audio`。原片和生成结果进入媒体索引；只有用户
明确保存或确认的替换对象才进入可复用资产库。物理文件、展示 URL、资产条目和生成
任务不能互相充当身份。

### 检索契约

首版检索必须在服务端执行并限制结果集：

```text
GET /media?kind=&q=&tag=&source_media_id=&project_id=&cursor=&limit=
GET /assets?kind=&q=&tag=&media_id=&cursor=&limit=
GET /recreation/projects/{id}/shots?source_pts=&q=&role=&cursor=&limit=
```

搜索文本覆盖显示名、别名、标签和人工备注；时间查询使用整数 PTS 与 time base，
不能把展示秒数作为精确条件。结果返回稳定 ID、授权缩略图投影、来源链摘要和权限
范围。客户端只负责排序/筛选当前小结果集，不能下载全库再搜索。

### 资产生命周期

上传原片 → 注册一个 `MediaRecord` → 分析产生 `ShotMediaBinding` 和证据媒体 →
用户确认/编辑 → 选择替换素材 → 生成任务快照引用 `media_id` → 生成结果注册新的
`MediaRecord` → 用户明确保存为 `AssetEntry`。删除采用软删除/引用计数检查；有镜头、
任务或资产条目引用时不能物理删除。签名 URL 只是可刷新投影。

### 迁移边界

本 PR 的复刻时间线暂存结构不应成为长期媒体身份。下一 PR 应先建立媒体索引和
查询接口，再把 `source_url`、证据 URL 和未来 H3 参考输入改为 `media_id` 快照。
在此之前只测试分析和时间线确认，不测试替换素材入库或跨镜头复用。

## 分阶段交付

1. 原片注册与分析：用户拥有的原片登记，ffprobe读取真实PTS、time_base、时长、画幅和音轨；FFmpeg生成切点候选、相邻帧证据与联系表。产物写入用户作用域。分析为异步任务，记录状态/错误，限制执行时间与资源。禁止使用shell拼接用户参数。
2. 切点确认：独立时间线界面显示候选切点前后帧；允许增删调整，区分detected/manual/confirmed来源。支持导入精确时间线。确认前不把抽样时间升级为精确切点。
3. 替换规划与提示词：显式选择保留对象和替换素材；按实际执行附件生成Picture/Video映射。H3六字段输出必须通过切点、引用、声音约束检查；黄色产品与红色礼盒分开建模。校验器冒号定义兼容问题需先补行为测试解决。
4. 生成与恢复：确认参数后调用现有生成任务；任务绑定复刻项目/镜头/版本。保存provider ID，刷新/重启恢复；新增并发重复提交保护，不能把PR #1的已有ID恢复等同于完整幂等。
5. 装配与验收：按确认时间线裁切、拼接和处理音轨。生成片过短必须明确报错或要求选择处理策略，不能静默拉伸/补帧。镜头边界从真实时间基准计算；展示与目标切点的偏差，严格模式以选定输出帧网格验收。

## 数据契约草案

- RecreationProject：id、owner_user_id、owner_profile_id、source_asset_id、source_fingerprint、timeline_revision、audio_policy、status。
- SourceAnalysis：probe信息、分析器版本/参数、候选时间戳、证据帧资产、时间戳来源、状态/错误；与原片指纹绑定，原片变化需重新分析。
- Shot：id、start_pts/end_pts及time_base、展示秒数、cut_source、确认状态、证据引用、保留/替换约束。
- GenerationBinding：shot/project/revision、ordered_reference_asset_ids、编译标签映射、提示词版本、现有task ID、输出资产。
- Assembly：输入版本清单、目标帧率/时基、音轨策略、输出资产、实际时长与切点核验结果。

新增分析模块建议置于 src/apps/recreation/，前端独立模块置于 frontend/src/components/modules/recreation/；接入前先检查各目录局部规则和既有路由/后台任务约定。数据库迁移、API契约与UI入口在第一实现切片内定稿，不提前建立重复owner或存储系统。

## 验收与执行顺序

### 首个切片的实现契约

- 使用现有 Studio owner 路径和签名媒体投影，新增 `output/recreation.sqlite3` 存储复刻项目 JSON；SQLite 事务与修订号控制并发，不更改现有项目数据库。
- `POST /recreation/projects` 接收 multipart 原片；`GET /recreation/projects[/{id}]` 查询；`POST /recreation/projects/{id}/analyze` 启动或重试；`PUT /recreation/projects/{id}/timeline` 以 revision 与 analysis_id 确认整数 PTS 切点。
- 首版仅本地上传 MP4/MOV/WebM/MKV，最大 256 MiB、60 秒、4096×4096、7200 解码帧。每个子进程有超时，禁止网络协议和播放列表格式。分析总租约 600 秒，过期任务允许显式重试，旧 attempt 不得发布结果。
- 原始文件 SHA-256 在分析前后及确认前检查；文件不可覆盖。分析产物按 attempt 隔离；未确认与已确认时间线分开保存。确认只接受严格递增的真实帧 PTS，结束边界由最后一帧时长或流 duration_ts 决定，不能使用末帧时间代替。
- 前端独立 `#/recreation` 入口提供原片上传、分析状态、前后帧、切点增删调整、精确秒数导入和确认。切点按源帧选择，显示实际时间；浮点秒数只用于展示，提交仍为整数 PTS。
- 本切片不提交 H3 请求，不实现替换素材和最终装配，不部署生产。变更通过 PR 交版本管理员审核。

- 下一实现切片：原片归属注册 → 真实PTS探测 → 候选切点与证据帧输出，不调用付费生成。先用合成硬切片段验证，再用经授权的桌面样本验收。
- 必测：变帧时间戳、无音轨、损坏文件、FFmpeg超时、跨用户文件/任务访问、路径穿越与符号链接、任务重试、过期原片指纹。
- 样本集成验收：15秒、四段；三个已确认切点可导入并原样保留；0.5秒与1秒联系表去重，末帧14.983333不误作结束边界。
- mocked provider测试只证明请求/状态行为；H3在线生成及视觉质量另记运行观察，不宣布统计稳定性。
- 基础回归命令：python -m pytest tests/test_studio_h3_submission.py tests/test_video_task_recovery.py tests/test_reference_submission.py tests/test_agent_skills.py tests/test_h3_prompt_check.py -q。
- 新模块测试随实现添加；发布前核对本地、GitHub、主机、容器及前端manifest，完成健康检查。当前文档不宣称已部署或已实现。
