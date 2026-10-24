# SupportFlow

电商售后 Agent 面试项目。基础框架、账号权限与双 JWT 登录已实现；商品、订单、Agent 和售后业务尚未实现，不得用模拟响应冒充这些功能已完成。

## 目录与边界

- `backend/api`：总路由 `router.py`、公共中间件与错误响应；第一版接口在 `v1/`，直接由 `api/router.py` 汇总，不添加版本目录内的二次汇总层。当前 URL 仍为 `/api/...`，目录版本不改变对外路径。
- `backend/core`：请求追踪及公共运行能力。
- `backend/db`：异步引擎、会话及后续数据访问。
- `backend/models`：后续数据库与领域模型。
- `backend/services`：后续业务规则与事务编排。
- `backend/agents`：后续 Agent 决策；工具放 `tools/`，知识技能放 `skills/`。
- `frontend/src/layouts`：用户端与管理端分开。
- `scripts`：Python 启动和运维入口。
- `tests`：后端验收测试。

依赖方向：配置/公共能力 → 存储与模型 → 业务/Agent → API。存储与业务层不依赖 FastAPI。新增实现必须有实际调用；不要添加通用 Repository、空工厂、未使用中间件或多 Agent 包装。

## 规则

- 配置统一 Pydantic Settings；根 `.env` 为本地配置，`SF_段__键` 可覆盖；密钥用 SecretStr，不输出配置原值。
- Python 依赖只维护根 requirements.txt，pyproject 复用。不要直接修改旧 medical-ragQA 项目。
- 用户指定使用 Conda，环境为 `D:\code\env\supportflow`；environment.yml 引用同一 requirements.txt，不创建 .venv，不修改已有环境。
- PostgreSQL 使用 `AsyncEngine → async_sessionmaker → AsyncSession`；每个并发任务独立会话，成功提交，错误或取消回滚。
- HTTP 有界准入与连接池上限分别管理；不得用增加连接池或进程数量代替实际压测。模型与外部工具将在真实调用入口设置独立预算，不在调用期间持有数据库事务。
- API 生命周期创建和关闭资源；禁止导入时连库、启动时自动建业务表。接入业务时加入版本化迁移。
- JWT 始终启用：短期 access + 轮换 refresh，角色从数据库读取，权限不可只靠前端。普通用户页面不显示后台入口，管理接口要求 admin。
- 不同标签页可同时登录不同账号：AT 仅内存，RT 按 sid 独立 HttpOnly Cookie，sessionStorage 只存定位 ID；禁止把 token 放 localStorage、sessionStorage 或 URL。账号变化必须取消/丢弃旧请求并重建页面，不能只换头像。
- 同 sid 刷新使用 Web Locks；不可用时明确拒绝不安全刷新，禁止并发轮换兜底。BroadcastChannel 只传播撤销 ID，不广播令牌；新标签可选择经过 Cookie 与数据库验证的已保持登录账号。
- refresh 轮换及重放撤销使用 PostgreSQL 行锁，退出及时撤销 access 对应会话，改密撤销该账号所有会话；失败状态必须提交后再返回错误，防止撤销被异常回滚。
- 登录限流使用 PostgreSQL 原子预算（账号与客户端两级），过期清理与预算更新分事务，禁止退回进程内字典或盲信任意 X-Forwarded-For。密码计算移出事件循环且受有界名额控制，取消请求后计算完成才归还名额。
- 业务表使用 Alembic 迁移。普通用户后续订单/工单接口必须校验真实数据归属，不能只验证签名。
- Agent 与 LangGraph 在第三阶段加入；LangGraph 届时是必需依赖，不写缺包兼容逻辑。
- 订单事实来自业务工具，政策来自有版本的知识库。模型不得直接写 SQL 或决定越权执行。
- 写操作需业务校验、幂等及确认流程；外部服务设置超时，重请求增加有界并发准入，避免无界任务。
- 不伪造支付、物流接入或业务性能；模拟数据与实际压测结果明确标记。
- Agent 采用单 Agent + 按业务划分的工具/服务；业务差异包含参数、规则、确认及状态流转，不只换工具名称。
- 用户要求操作白盒：接入 Agent 时同步提供真实的步骤/工具事件、结果摘要、耗时、引用、确认与失败记录。不得展示或存储私有推理、密钥、完整系统提示词及未经筛选的原始载荷；字段按工具白名单输出。
- 执行记录必须校验会话/业务归属。通过 PostgreSQL 短事务保存权威事件，SSE 支持去重与续读，有界缓冲；模型输出不能伪造工具成功，断线不能触发重复业务操作。具体设计见 docs/agent-execution.md。

## 验证

```text
python -m pytest -q
python -m ruff check backend scripts tests
python -m ruff format --check backend scripts tests
cd frontend
npm test
npm run format:check
npm run build
```

用户此前指定 GPT-5.6 Luna 审查代码；工具支持时使用该模型，不得用其他模型审查后声称使用了 Luna。

