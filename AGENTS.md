# SupportFlow 项目说明

本仓库是软件开发业务的需求接待与售后协作 Agent，当前目录为 `D:\code\python\project`。不要修改旧医学项目。读本文件即可了解当前规则，不再沿用已移除的电商业务说明。

## 产品与真实流程

- 面向不一定懂软件的客户。客户先点击“开始需求沟通”，不要求先填项目名称、完整说明或 Java/Python 技术栈。
- 业务顾问顺着客户本次问题自然解释、给建议和比较取舍；仅在答案影响方案时追问，不要求每轮总结或提问。必要时问1至2个重要问题，避免长问卷和重复追问，不以填满字段为目标。
- `delivery_projects.phase=discovery` 是沟通记录，尚未立项；已知事实和待确认事项保存在 discovery JSON。现有表名用于延续数据，不代表客户进入时必须创建正式项目。
- 需求访谈默认一次顾问模型调用，自然回答可不调用工具；有新事实或起草决定时调用record_consultation，记录工具按facts单次填写值和来源，由程序生成notes/sources，避免双份措辞不一致；reply可选，缺失不丢弃事实。自然回应不覆盖为固定追问，补充回复限制输出预算900。来源引用source_catalog逐项验证，错误事实补丁不提交，但保留对话。意愿引用不精确只忽略该控制信号，不使整轮失败。已删除assess节点，不再评估信息齐全度。memory_pending标记未完成纪要，并明确提示用户。人工断点恢复时以本轮已鉴权PG快照中的纪要为准，不用旧checkpoint遮蔽修复内容。主流程保留原节点名用于历史断点恢复。
- 主沟通图保留真实LangGraph interrupt/Command恢复与异步PG检查点；等待客户释放执行名额。起草由独立DraftAgent通过原生工具循环自主决策，不用固定业务阶段串联。完整流程见docs/requirement-consultation.md。
- 技术偏好为开放文本，支持任意语言/框架/平台或部署约束；不局限Java/Python，未选型通常不阻塞业务草案。
- 草案仅按本轮客户要求生成，不再因信息齐全自动起草。明确完整起草命令直接读取已存信息；其他表达由本轮顾问引用原话触发。接口文档、技术选型和支付资质不作为草案门槛。
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
  agents/orchestrator.py        执行生命周期、并发与正式项目工具循环
  agents/client.py / limits.py  异步模型请求、用量与预算
  agents/consultation/graph.py  流程连接、检查点装配与Command恢复入口
  agents/consultation/nodes.py  沟通、按需起草决策与人工断点
  agents/drafting/service.py   输入交接、事实附录和草案输出
  agents/drafting/agent.py     独立模型会话与有界工具决策循环
  agents/drafting/tools.py     大纲、写章、搜索、查看正文与结束工具
  agents/drafting/state.py     Pydantic统一起草状态，每次执行独立
  agents/drafting/prompts.py   子Agent职责、角色称呼与建议边界
  models/drafting.py           起草意愿与工具参数校验
  agents/tools/                registry/project/discovery/drafts/web_search
  api/router.py                唯一总路由，main 只挂载它
  api/v1/                      auth、health、projects（含consultations入口）
  core/                        安全、请求追踪
  db/session.py                异步引擎、会话工厂、独立事务
  db/project_store.py          沟通/项目、版本、工单、执行记录的短事务
  db/project_admin_store.py    管理员跨项目统计与分页摘要，数据库角色复核
  db/checkpoints.py            项目隔离的LangGraph异步检查点，最近30份/线程
  db/cache.py                  可丢弃读取快照、短TTL、同键合并与回源限流
  db/tool_store.py             工具总开关、版本和幂等审计
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
- Redis只缓存执行记录快照和后台汇总。命中前在PG复核身份/项目归属；run键含账号/项目/run/cache_version，所有状态变更在同一事务递增版本。业务成功提交后更换汇总代次，旧回填写旧代次；失效失败最多保留短TTL。断点、权限、JWT、幂等和业务锁仍在PG。Redis异常150ms超时并熔断5秒，完整记录回源每进程最多8并发/等待250ms；不在SQL事务中访问Redis。完整说明见docs/cache.md。
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

- LangGraph是必需依赖，主沟通图负责等待与恢复；起草是generate_prd交接给独立DraftAgent的真实模型工具循环，有私有会话、State和终止条件。不增加子图套壳或缺包兼容分支。
- 工具仅整理纪要/草稿；发布、审批、立项和关闭仍由真实角色确认。模型不能直接写SQL、越权、自动承诺工期费用或伪造开发测试成功。
- 纪要列表有序去重累积，已解答问题仅通过resolved_questions移除，客户明确纠正的事实通过remove_facts移除；本轮多个补丁按顺序保留，禁止整字段覆盖历史列表。技术偏好组件可在同一客户消息内组合，当前输入优先历史；旧来源仍逐项验证，标量替换不要求删除旧标量。
- 普通沟通的消息对与已验证纪要同事务保存。起草前先独立提交消息对、已验证纪要及discussion_run_id回执，执行保持running；成功后按新版本原位更新本轮助手消息并提交草案，不重复消息或事实补丁。子Agent失败保留已保存沟通、原有草案和已核实事实；格式、网络、预算不足分别标记，不能假称完成。权限/版本异常拒绝写新草案。取消或超时后已保存的对话给出起草中断说明。
- 起草主流程只交接已存纪要、客户原话、本次要求和已有参考。独立DraftAgent自己选择plan_draft、write_prd_section、read_draft、web_search、finish_draft，读取真实工具结果再决定下一步；可直接输出正文，不按条目数硬编码分支，也不强制规划/搜索/写章顺序。没有自动全篇审计、重复历史抽取或模块JSON正文。
- 每次起草创建独立Pydantic DraftState，集中管理只读客户资料、大纲、角色称呼、章节、失败、参考、待确认、私有消息和状态。写章只传section_id，从同一State读取大纲，不要求模型重抄章名/重点；本次章名与序号可映射到规范ID。工具共享该实例，不使用全局单例；输入深拷贝，建议不回写客户纪要。LangGraph主图仍用TypedDict传递节点数据。章节状态是本次执行内存数据，未声称已实现进程中断后的自动续写。
- 默认每进程4个后台执行，起草不再占额外槽位。正式项目5模型/8工具/40000token/90秒；起草共享累计8模型/8次处理工具/64000token/150秒，所有控制和写作调用都计入。章节输出2400token、单次60秒，从外层真实截止时间预留12秒保存；预算耗尽停止请求。模型可按需一次安排多个工具，程序顺序执行，已有章节可用一次本地finish_draft收尾，不调用外部服务也不占处理额度；无效收尾消耗该次机会。结束之后不再执行动作。
- 同章本轮最多尝试一次，重复调用返回已有结果，不再次请求写作模型。单章失败把观察结果交还子Agent，其可继续别章或结束；失败/缺失章节保留占位，预算或模型故障保留已完成内容。角色称呼统一；客户要求生成后，未说明的技术栈、字段、校验、异常、部署与验收由Agent主动选择合理方案，不只复述事实。补充方案分类保存到consultation.draft_assumptions，和客户discovery事实分离；后续客户明确技术偏好时，旧的自动实现建议不再进入起草上下文（包括邮件库等配套组件），不能保留客户已排除的运行环境。正文自然写作，默认方案统一列为自动补充/可调整，不能处处待确认；只有外部授权、客户内部信息和冲突规则保留待确认。完整新草案不机械继承旧访谈问题，partial仍阻塞。事实逐项附录。权限、版本和外部取消不转成部分成功。无资料不编造，正文120000字符、待确认100项。旧draft_ledger仅承接历史。
- 联网默认关闭，管理员总开关+客户本轮联网开启后，顾问与Draft Agent均可自主调用，不逐次确认。顾问每轮最多查一次，起草子Agent最多2个不同公开主题，不强制搜索。web_search仅接收服务端枚举主题，由服务端生成公开查询，不发送客户原话或身份资料。执行前再次查总开关；失败继续本地讨论。公开资料仅作为有来源的建议。
- 分阶段事务均复核账号、归属、项目版本和运行回执；最终保存后保持running至HITL检查点写完。检查点故障依靠已提交业务回执补齐，不重复消息或草案。检查点线程与项目隔离，不公开原始状态。
- 项目行锁+运行记录唯一索引提供跨进程互斥。任务目前在API进程运行，不声称已有持久化队列。180秒中断回收持run锁重新检查，禁止覆盖已完成结果。
- 后台事件保留实际工具/纪要保存/模型轮次、耗时与用量，用户只看到易懂的业务步骤。不展示私有推理、完整提示词、密钥或原始载荷。Markdown白名单清洗，Mermaid strict，不加载远程图片。
- 用户界面以日常业务语言展示实际步骤：模型调用、工具用途、整理信息及等待补充；不显示HITL、人工断点、JSON、Token、内部节点/函数名或原始事件摘要。耗时显示秒数；真实事件和计量保留供后台诊断，不能制造调用。新生成的追问与失败提示也使用客户能理解的表述。
- 沟通使用ConversationPanel连续聊天窗口：历史独立滚动，输入区固定在面板底部，高度适配当前屏幕；上翻历史时不抢滚动。执行记录放在每轮助手消息内，“正在思考”可展开真实事件，空闲时没有常驻执行面板。Skill/MCP仅展示真实类型事件，不制造调用记录。
- 聊天工作区由布局flex分配剩余屏幕，不按顶部文档坐标或760px上限计算高度。工具栏/项目信息紧凑，输入区两行；窄屏业务纪要按钮展开，客户可选择本轮是否联网，无逐次查询确认。管理员详情同路由前进/后退也复核未确认写操作，按projectId/tab/item刷新并丢弃旧响应。
- 新消息对与ProjectRun通过run_id同事务关联，详情返回当前消息对应的执行记录及最近5次执行；旧无关联消息保留原文，不按相同问题文字猜测归属。请求尚未确认时显示正在发送/结果待确认，与实际模型执行区分。
- 当前只有文本资料与Markdown导出。没有附件解析、向量RAG、代码仓库集成、自动开发、自动报价或自动排期；有真实需求和调用再加。

- 需求文档按Markdown段落/列表正常折行；p/li不使用pre-wrap保留HTML排版空白，嵌套列表显式紧凑边距，代码块保持pre。排版调整须在真实浏览器检查现有长文档与新生成文档。

## 启动与验证

```powershell
conda activate D:\code\env\supportflow
python scripts/migrate.py
python scripts/start_api.py
# 另一个终端在frontend中执行 npm run dev
# 首次正式部署：python scripts/create_user.py admin --role admin
```

API8091、前端5183、PostgreSQL15432。页面只通过/api代理。不要把开发验证账号和样例数据放入正式初始化。
Redis16379由docker/compose.yaml启动，本机SF_CACHE__ENABLED=true；默认代码配置关闭，示例.env开启。管理员/api/admin/cache可查看当前进程缓存命中/故障计数。迁移0015增加run缓存版本。

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
