# SupportFlow 项目说明

本仓库是软件开发业务的需求接待与售后协作 Agent，当前目录为 `D:\code\python\project`。不要修改旧医学项目。读本文件即可了解当前规则，不再沿用已移除的电商业务说明。

## 产品与真实流程

- 面向不一定懂软件的客户。客户先点击“开始需求沟通”，不要求先填项目名称、完整说明或 Java/Python 技术栈。
- 业务顾问先了解客户在做什么业务、现在怎么处理、遇到什么问题，再随回答逐步追问参与人员、关键流程、业务规则与预期效果。每轮只问1至2个重要问题，避免长问卷和重复追问。
- `delivery_projects.phase=discovery` 是沟通记录，尚未立项；已知事实和待确认事项保存在 discovery JSON。现有表名用于延续数据，不代表客户进入时必须创建正式项目。
- Agent 用 `remember_requirements` 累积客户明确提供的信息，不把建议当确认事实；后续轮次带入纪要与近期对话。客户要求整理或同意整理建议后再调用 `save_draft`。
- 需求草案：草稿 → 提交评审 → 客户与负责人分别确认。问题未补齐不可确认，编辑重置双方签名。双方确认才发布 V1，并把 phase 转成 project；名称取双方确认的需求书标题，保留沟通历史。
- 正式项目后支持需求变更、开发任务、交付验收和售后。变更写明新增/修改/删除，并保存变更后完整需求书；双方确认发布新版，旧版不可覆盖。
- 故障/咨询/任务/交付记录：草稿 → 提交 → 负责人处理 → 提交处理结果与依据 → 客户验收关闭；客户可退回继续处理。任务/交付仅负责人创建与维护。
- 处理中可追加补充说明，原申请正文与历史意见保留；需求/变更可并排查看当时基线与当前完整正文，不用模型重新生成旧版内容。
- “管理记录”支持名称检索、重命名、单条/批量移入回收站及恢复。客户仅管理本人沟通记录，正式项目由管理员管理；回收站保留对话、文档、处理历史和独立操作审计，不提供永久删除入口。手动命名不被Agent纪要或需求发布覆盖。
- 当前角色：admin 为负责人，普通客户和 staff 仅本人记录；staff 没有跨项目权限。客户看不到后台入口。
- 管理员首页独立于客户聊天：/admin管理总览，/admin/projects客户与项目，/admin/reviews需求审核，/admin/tasks开发任务，/admin/deliveries交付验收，/admin/service售后工单；保留账号与运行状态。按客户/项目/状态分页筛选，进入/admin/projects/:projectId后才使用项目工作区。列表操作精确定位tab/item，不直接批量批准。管理员访问用户根页转/admin。
- 正式业务管理风：蓝灰配色、紧凑字号和间距、标准空状态；不用宣传标题、装饰符号、开发路线图或默认示例项目。不把验证记录留在客户业务中。

## 代码结构与依赖

```text
backend/
  agents/orchestrator.py        LangGraph 单 Agent、原生工具调用、后台执行与预算
  agents/tools/projects.py      项目资料、纪要、草案、版本比较工具
  api/router.py                唯一总路由，main 只挂载它
  api/v1/                      auth、health、projects（含consultations入口）
  core/                        安全、请求追踪
  db/session.py                异步引擎、会话工厂、独立事务
  db/project_store.py          沟通/项目、版本、工单、执行记录的短事务
  db/project_admin_store.py    管理员跨项目统计与分页摘要，数据库角色复核
  db/auth_store.py             账号、可撤销登录、限流
  db/identity.py               写操作事务内复核真实身份
  db/migrations/               Alembic历史链，保留历史迁移，不重写已执行版本
  models/                      auth、orm、projects
  services/auth.py             JWT登录与账号业务
frontend/src/
  views/ProjectsView.vue        沟通优先的客户/负责人工作台
  views/AdminProjectsView.vue   独立管理总览、项目及业务处理列表
  components/RequirementDocument.vue  安全Markdown与Mermaid预览
  style.css                    全局布局、鉴权、账号、状态样式
  workspace.css                当前业务工作区样式
scripts/                       启动、迁移、正式账号创建
```

依赖方向为配置/公共能力 → 模型与存储 → 业务与 Agent → API。存储与业务不得依赖FastAPI、Starlette或main。不增加空工厂、通用Repository、多Agent套壳或无调用模块。旧电商运行代码、测试与脚本已移除；历史表保留，旧接口不再挂载。清理前的未提交文件在忽略提交的 storage/cleanup-archive 中可恢复，不导入运行。

## 工程与安全

- Conda环境：`D:\code\env\supportflow`，不用.venv。依赖只维护根requirements.txt，pyproject/environment.yml复用。
- 配置统一Pydantic Settings，根.env本机配置，SF_段__键覆盖；密钥SecretStr，不输出配置/供应商异常原文。
- PostgreSQL采用 AsyncEngine → async_sessionmaker → AsyncSession；每个并发操作独立短事务，成功提交，异常/取消回滚。模型和外部工具期间不得持有SQL事务。
- API生命周期创建并关闭资源；无导入时连库、启动自动建业务表或自动造数据。结构变更用Alembic；未知历史表不被autogenerate擅自删除。
- JWT access+轮换refresh始终启用；角色与会话有效性从数据库复核。账号停用、退出、改密撤销旧会话。
- 不同标签页支持不同账号：AT仅内存，RT按sid独立HttpOnly Cookie，sessionStorage仅存sid。禁止令牌进URL/localStorage/sessionStorage。
- 同sid刷新用Web Locks；不支持时拒绝不安全刷新。BroadcastChannel仅传撤销ID。账号切换取消/丢弃旧请求并重建页面。
- 登录限流用数据库原子预算；密码计算移出事件循环并限制并发。refresh轮换/重放撤销使用行锁，撤销失败结果先提交再返回错误。
- 写操作锁顺序 user → login → project → item/run，1.5秒锁等待；事务中复核身份/归属。越权与不存在统一404。
- request_id按操作者隔离，校验参数指纹；版本号防止过期覆盖。业务与审计同事务；未知写结果保留原请求重试，禁止换ID重复提交。
- 记录管理以Project.version防过期批量操作，锁项目与运行记录；有未结束Agent执行时拒绝删除。批量变更全部校验后单事务提交；删除记录的详情、文档与Agent入口均404，恢复后沿用原ID和历史。删除当前记录清空其输入/执行状态并选择下一条，重命名保留输入。
- 列表每页≤100、offset≤10000，多取一条判断下一页；异步读取带项目代次，防止旧响应覆盖新选择。
- 开始沟通的防重锁覆盖创建及后续读取全程；切换记录保留原工作区，详情/模型状态就绪后一次更新，不先清空造成闪动。普通客户当前未使用的空白沟通直接复用并保留输入；创建成功但读取失败只重开原记录，不再创建。

## Agent执行

- LangGraph是必需依赖；单Agent按上下文选工具，禁止关键词硬路由或缺包兼容分支。
- 工具仅整理纪要/草稿；发布、审批、立项和关闭仍由真实角色确认。模型不能直接写SQL、越权、自动承诺工期费用或伪造开发测试成功。
- 纪要列表有序去重累积，已解答问题仅通过resolved_questions移除，客户明确纠正的事实通过remove_facts移除；本轮多个补丁按顺序保留，禁止整字段覆盖历史列表。
- 纪要、草稿、对话成对原子提交；任一失败不保存孤立问题或半份纪要。无模型配置时明确提示，不用模板冒充模型。
- 默认每进程4个后台执行、每轮最多5次模型调用/8次工具调用/3份草稿；限制外部超时、整轮时长、上下文与token预算。请求前保守预留输入、限制输出；usage缺失时回退计量，超额不保存。
- 项目行锁+运行记录唯一索引提供跨进程互斥。任务目前在API进程运行，不声称已有持久化队列。180秒中断回收持run锁重新检查，禁止覆盖已完成结果。
- 显示实际工具/纪要保存/模型轮次、耗时与用量，不展示私有推理、完整提示词、密钥或原始载荷。Markdown白名单清洗，Mermaid strict，不加载远程图片。
- 沟通使用ConversationPanel连续聊天窗口：历史独立滚动，输入区固定在面板底部，高度适配当前屏幕；上翻历史时不抢滚动。执行记录放在每轮助手消息内，“正在思考”可展开真实事件，空闲时没有常驻执行面板。Skill/MCP仅展示真实类型事件，不制造调用记录。
- 聊天工作区由布局flex分配剩余屏幕，不按顶部文档坐标或760px上限计算高度。工具栏/项目信息紧凑，输入区两行；窄屏业务纪要按钮展开。管理员详情同路由前进/后退也复核未确认写操作，按projectId/tab/item刷新并丢弃旧响应。
- 新消息对与ProjectRun通过run_id同事务关联，详情返回当前消息对应的执行记录及最近5次执行；旧无关联消息保留原文，不按相同问题文字猜测归属。请求尚未确认时显示正在发送/结果待确认，与实际模型执行区分。
- 当前只有文本资料与Markdown导出。没有附件解析、向量RAG、代码仓库集成、自动开发、自动报价或自动排期；有真实需求和调用再加。

## 启动与验证

```powershell
conda activate D:\code\env\supportflow
python scripts/migrate.py
python scripts/start_api.py
# 另一个终端在frontend中执行 npm run dev
# 首次正式部署：python scripts/create_user.py admin --role admin
```

API8091、前端5183、PostgreSQL15432。页面只通过/api代理。不要把开发验证账号和样例数据放入正式初始化。

修改中只运行受影响检查，收尾集中检查一次，通过且代码未变不重复跑；只有并发相关改动才补相应并发检查。审核使用用户指定的 GPT-5.6 Luna，medium中档：主审一次，修复后只复核相关部分。

```powershell
$env:SUPPORTFLOW_TEST_POSTGRES="1"
python -m pytest -q
python -m ruff check backend scripts tests
python -m ruff format --check backend scripts tests
cd frontend
npm test
npm run format:check
npm run build
```

PostgreSQL测试在隔离schema运行并清理，真实模型验收也使用临时schema，禁止留下示例项目污染客户列表。
