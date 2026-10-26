# 开发协作规范

本仓库是SupportFlow软件需求接待与项目协作Agent，工作目录D:\code\python\project。不要改动另一个medical-ragQA仓库。本文给开发会话提供稳定规则；功能细节与图见[文档导航](docs/文档导航.md)。

## 产品约定

- 面向不懂软件实现细节的客户，先自然沟通业务，必要时问1至2个关键问题；不用长问卷或要求先填完整项目表。
- phase=discovery表示沟通记录，尚未立项。只有客户本轮明确要求才起草，不因字段齐全自动生成，不让技术选型和接口文档成为起草门槛。
- 草案面向工程师：充分展开角色、功能步骤、字段、接口、状态、异常、并发一致性、技术架构、部署和验收。
- 用户未选的设计就近标【自动生成】，表格有来源列，重要默认决策汇总到assumptions；客户事实与自动建议分开。默认方案不是阻塞性待确认问题。
- 后续客户指定的技术和规则覆盖旧自动建议；不能恢复客户排除的业务。正文允许SQL/配置参考，标自动生成、待工程验证，不虚构已开发/实测，不填真实密钥或固定生产密码。
- 局部错误应保留详细稿修订，不因此把全文降成纪要。生成完成、语义通过、双方确认发布分别验收。
- 客户页面为连续聊天，输入区固定、历史独立滚动；本轮执行步骤放助手消息内，不显示空执行面板或私有推理。
- 用户只见易懂步骤与秒数，不见HITL/JSON/Token/内部函数名等术语；后台保留真实事件，不能制造Skill/MCP调用。
- 界面用正式管理风，蓝灰配色、紧凑布局；Markdown白名单清洗，Mermaid strict，不加载远程图片。
- 不修改现有客户数据做测试。独立验证记录可恢复清理；用户明确保留供审阅的草案保持可见，失败历史不伪装成首次成功。

## 业务与权限

- user和staff当前仅本人记录；admin是负责人并拥有跨项目后台权限。普通用户不显示后台入口，服务端仍逐请求校验。
- 需求/变更：draft → submitted → 双方分别确认 → approved，才发布V1/V2并更新基线。待确认非空不能确认，编辑退回draft且清空双方签名。
- 初始草稿不显示V0；version是防覆盖计数，revision/published_revision才是正式业务版号，已发布正文不可覆盖。
- 变更包含新增/修改/删除说明及变更后完整需求书，保留旧基线；差异比较是正文diff，不是代码影响分析。
- 故障/咨询/任务/交付：提交 → 负责人处理 → 提交结果与依据 → 项目客户验收关闭或退回。task/acceptance由负责人创建维护，客户负责验收。
- 驳回由负责人，撤回由创建者，必须说明原因。补充说明与处理历史保留，不覆盖原申请。
- 确认本版、验收关闭、驳回和撤回使用业务窗口内二次确认，默认焦点在取消；快照项目/记录版本及原因，版本或编辑变化使旧确认失效。
- 记录管理支持检索、手动命名、单条/批量回收和恢复，无永久删除入口；有运行任务时禁止删除。客户只管理本人沟通记录，正式项目由管理员管理，恢复保留原ID和历史。
- 手动命名不被Agent或发布覆盖。切换记录保留输入直至新详情就绪；开始沟通防重覆盖创建和后续读取，读取失败重开原记录，不重复创建。

## 目录与依赖

```text
backend/agents/
  runtime.py                 生命周期与业务图分派
  common.py                  共享纯函数
  consultation/              graph.py / nodes.py / prompts.py / state.py
  drafting/                  graph.py / nodes.py / prompts.py / state.py
  project/                   graph.py / nodes.py / prompts.py / state.py
  tools/                     唯一工具目录：drafting.py / project.py / web_search.py
backend/core/llm/             模型工厂、客户端、用途配置
backend/api/router.py        唯一总路由
backend/api/v1/              HTTP薄接口，URL当前以/api开头
backend/db/                  异步PG、缓存、迁移、检查点与业务事务
backend/models/              业务、状态与参数校验
backend/services/            鉴权业务
frontend/                    用户端与管理后台
scripts/                     启动、迁移、账号维护
```

依赖方向：配置/公共能力 → 模型与存储 → 业务/Agent → API。存储、Agent不得依赖FastAPI、Starlette或main；不增加无调用工厂、空Repository、多Agent套壳或旧入口兼容壳。

业务Agent只保留graph/nodes/prompts/state，不新建agents/shared、backend/tools或散落的agent/service/runtime套层。工具统一复用agents/tools，图装配和节点实现分离。测试/输出资料放storage或tests，不塞进Agent目录。

## 数据、事务与安全

- Conda环境D:\code\env\supportflow；不用.venv。Python依赖只维护requirements.txt，pyproject/environment.yml复用。
- 配置用冻结的Pydantic Settings、根.env及SF_嵌套覆盖；密钥SecretStr，不输出本机.env、供应商原文、Cookie或令牌。
- AsyncEngine → async_sessionmaker → AsyncSession。每个操作独立短事务；成功提交，异常/取消回滚。模型/搜索期间不能持有SQL事务，不在线程里执行SQL。
- 生命周期创建和关闭连接；导入不连库，不启动自动造账号/示例业务。结构变更用Alembic，不擅自清理未知历史表；project_operations.action需要64字符容纳完整审计ID。
- JWT access/轮换refresh始终启用；角色、账号和登录有效性从PG复核，退出/停用/改密撤销旧登录。固定HS256，校验用途和标准声明。
- access只在标签页内存，refresh按sid独立HttpOnly/SameSite Strict Cookie，sessionStorage只存sid。JWT不能进URL/localStorage/sessionStorage。
- 同sid刷新用Web Locks；不支持时拒绝不安全刷新。BroadcastChannel只传撤销ID，账号切换取消旧请求、丢弃旧响应并重建受保护页面。
- Origin、受信任代理、账号/客户端登录预算由服务端控制；Argon2计算移出事件循环并限制并发，不在计算时占SQL会话。
- 锁顺序user → login → project → item/run，锁等待1.5秒；事务中复核身份/归属，越权与不存在统一404。
- request_id按操作者隔离，参数指纹校验；expected_version防过期覆盖。业务和审计同事务；未知结果用原ID/参数重试，不换ID重复提交。
- 业务列表分页有界，limit最多100、offset最多10000；同条历史先按item_id鉴权筛选再取最近60条，不先截项目历史后前端过滤。
- Redis只缓存run快照和管理汇总；命中前PG查权限/版本，状态变更递增cache_version。汇总提交后更换代次，旧回填不能覆盖新代次。
- Redis操作在SQL事务外，150ms超时、熔断5秒；完整回源默认8并发/250ms等待。PG不可用不凭缓存绕过授权，缓存不保存JWT/幂等/检查点。
- 短期缓存和本进程信号量不能代替业务锁；多实例不声称共享模型额度或缓存单飞锁。

## Agent执行边界

- LangGraph必需。主沟通7节点，实际名称为extract、decide_draft、save_discussion、draft_agent、wait_user、persist、human_input；没有Assess节点。
- 普通沟通默认一次顾问调用，可直接解释；有新事实用record_consultation，来源与Schema逐项检查。错误补丁不覆盖原纪要，原话保留并标memory_pending；补充回复可额外调用，不能隐瞒。
- 列表补丁累计去重；resolved_questions解决旧问题，remove_facts仅用于客户明确纠正。技术组件可组合，当前输入优先历史；来源存在不证明语义必然正确。
- 起草前先保存沟通、纪要与discussion_run_id，成功后原位更新本轮助手消息及草案；最终提交复核运行回执、权限与版本，不重复消息。
- 主图有真实interrupt/Command和PG检查点；human_input等待释放执行名额，恢复采用最新鉴权PG快照，不被旧checkpoint遮蔽。
- 起草是独立decide/tools/finalize子图，原生工具由模型自主选择；不能改回固定业务流水线。Pydantic DraftState每次深拷贝并共享给工具；各图用标准库TypedDict传递状态。
- 起草线程draft:run_id与主图线程隔离；逐工具同步检查点，恢复核对输入指纹、操作者/项目/版本，恢复计数和绝对截止时间，已写章不重写。
- 初次AGENT、后续CONTINUE精简调度，写章仍用完整SECTION/RULES。精简不能漏掉费用/工期、权限、来源、不得伪造验证等限制。
- 待执行工具先执行；pending清空且所有计划章齐全、无失败时本地finalize，避免重复调度。原有partial不能被提升成完整。
- 同章本轮至多尝试一次；单章失败交还真实观察，可继续其他章，结束时保留缺项。权限/版本/取消不作为普通内容故障吞掉。
- 正式项目为agent/tools两节点，当前无图检查点或interrupt，也不调用Draft子图；工具准备稿成功收尾后统一事务保存。
- 工具不能执行任意SQL、代码、审批、发布或关闭，不能承诺费用/工期或虚构已开发。文档里的SQL示例与执行数据库命令是两回事。
- 默认每进程4后台任务。正式项目5模型/8工具/40000 Token/90秒；起草共享12模型/8处理工具/64000 Token/150秒，单章4000输出/单次60秒，预留12秒保存。输入UTF-8预算预留不取消。
- generate_prd交接计一次工具；已有章的一次本地finish不额外占处理额度，无效也消耗机会。usage只按可获得的响应计量，不当云账单保证。
- 联网默认关闭，管理员总开关与客户本轮同时允许后自主使用，执行前复核。顾问一次、起草最多两个公开主题，失败继续本地；项目协作当前不提供搜索。
- 查询仅发送服务端枚举公开主题，不发送客户原话、身份或凭证。当前DuckDuckGo HTML摘要只作有来源建议。
- 后台任务在API进程内，PG运行互斥；超过180秒未结束可加锁回收。没有持久队列、自动重启调度或模型请求恰好一次保证。
- 实际模型与purpose保留在事件中；同model ID共用最小并发额度，不用换用途规避限制；429/配置错误不隐式切换模型。
- 工厂内部统一用ChatOpenAI异步适配器，Agent不得直接取裸模型绕开额度；回复与章节走astream，规划/JSON仍整包校验。SDK自动重试关闭，流式取消/错误关闭响应并释放模型名额。
- SSE以fetch携带标签页鉴权头读取原run，每秒复核身份/权限、25秒轮换；最多64连接/每登录2连接，不长期占HTTP准入名额。预览只发公开正文，40000字符上限、Redis180秒临时副本，不逐Token写PG；完成后以PG结果替换。断线退回查询，不重复提交业务。

## 启动与文档维护

首次启动、端口和示例配置见[README](README.md)。API8091、前端5183、PG15432、Redis16379；Compose目前只起PG/Redis。更改代码/配置后重启API前确认无活动执行，不打断用户运行。

docs文件名与标题统一中文；根README.md、AGENTS.md保留工具约定。各Agent文档都要有业务流程、真实节点图、节点表、模拟轨迹、失败与恢复边界，节点图以代码注册为准。工具、业务审批、API与图节点必须分开说明。

## 验证与审查

按变动范围选择检查，已通过且未再修改不重复整套。文档变更检查链接、Mermaid和实现对应，不重启API或再次付费调用模型。真实PG/Redis用临时schema/前缀，测试数据不得污染正常初始化。

代码审查用用户指定GPT-5.6 Luna、medium，一次主审、修复后定向复核。审查不代替真实运行，模拟不代替实测，不能把不同版本的通过数量拼成一次最新全量结果。具体检查见[验证指南](docs/验证指南.md)。
