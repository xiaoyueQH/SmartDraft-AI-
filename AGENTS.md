# SupportFlow

电商售后 Agent 面试项目。基础框架、双 JWT、商品入库/编辑/上下架/回收站恢复、单商品下单与库存预占、本人/后台订单查询、确认取消、演示付款/发货/收货、整单退货审核/寄回/验收补库/演示退款关闭已实现。真实支付/退款/物流、换货和 Agent 尚未实现。演示数据已落 PostgreSQL；页面仅保留必要的演示环境说明，不在商品名或卡片重复“模拟”字样，不得冒充真实交易或已完成的 Agent。

## 目录与边界

- `backend/api`：总路由 `router.py`、公共中间件与错误响应；第一版接口在 `v1/`，直接由 `api/router.py` 汇总，不添加版本目录内的二次汇总层。当前 URL 仍为 `/api/...`，目录版本不改变对外路径。
- `backend/core`：请求追踪及公共运行能力。
- `backend/db`：异步引擎、会话及后续数据访问。
- `backend/models`：账号 ORM、commerce 商品/订单 ORM、fulfilment 履约售后 ORM、orders/workflow 查询与命令视图。
- `backend/services`：鉴权、订单范围校验与业务编排。
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
- 订单查询固定按当前用户归属过滤；管理员跨用户查询在服务层再次校验角色。越权订单与不存在订单均为 404。金额使用整数分，订单明细保存购买时名称/SKU/单价快照，不从当前商品反算历史订单。
- 商品/订单列表每页不超过 100，offset 不超过 10000，稳定排序且用多取一行判断下一页，不做每次全表计数。详情批量读取物流事件，不逐运单查询。
- 演示数据通过 `scripts/seed_demo_orders.py` 显式初始化，锁定 user 账号并在一个事务写入；确定性 ID 保证重复运行不覆盖业务进度。API 启动不自动造数据。
- 管理端 `#/admin/catalog` 使用 `services/mock_catalog.py` 的 24 款虚构候选，只读搜索不自动写库。管理员核对名称、说明、整数分售价与初始库存后入库；商品和 `catalog_imports` 来源/操作者/候选快照同事务保存。同来源候选只允许入库一次，重试返回首次商品，不重置售价、不累加库存。写事务按 user → login 共享锁重查有效登录与 admin 角色，锁等待最多 1.5 秒。不得把模拟来源标成淘宝或真实商品。
- 商品写操作按 user → login → product 锁定；编辑、上下架、移入回收站和恢复均要求 expected_version，保存 ProductChange 审计和操作 ID 幂等结果。下单扣库存、取消归还库存、演示预占脚本均递增商品版本，过期编辑不得覆盖库存。软删除保留商品/来源/历史订单，恢复后默认下架，普通用户只看未删除且上架商品；回收站商品不得通过重复入库自动恢复。
- 管理员可通过 `POST /api/admin/catalog/products` 手动创建自己的商品，SKU 规范化大写且唯一；request_id 按账号隔离并校验参数指纹。商品与 `catalog_imports` 中 manual 来源、原输入和指纹同事务保存，`Product.is_demo=False`；候选商品保留原来源。重复请求返回原商品，SKU 重复返回 409，回收站商品不得通过重复请求恢复。
- `POST /api/orders` 单商品下单支持候选及手动商品，交易仍为演示环境，`Order.is_demo=True` 与商品来源标志分开；取消按真实预占释放，不比较商品来源和订单演示标志。request_id 按真实账号隔离，同 ID 不同参数返回 409。事务按 user → login → product 锁序校验状态、价格与库存，再原子保存订单、购买快照、预占并扣库存；提交后再返回订单详情。付款渠道仅为 demo，必须明确标识演示，不得伪造真实渠道成功。
- 手动新增与候选入库统一使用原生 dialog，禁止通过改网格列数插入侧栏。保持稳定滚动条槽、背景隔离、关闭后焦点和滚动位置；未知保存结果时禁止关闭并保留原请求体重试。
- 取消采用两阶段：预览确认单绑定订单、操作者、登录 sid、原因及快照哈希，5 分钟有效；明确确认后重新锁定核查。确认 ID 也是幂等键，完成后原 ID 重试返回原审计，不重复释放库存。网络结果未知时前端保留该 ID，只允许手动重试；不得自动换新 ID 重发。
- 取消写事务锁顺序为操作者 user → login → order → confirmation → 本订单 reservations → 按 ID 排序 products。会话、角色在事务内重查；库存释放、预占标记、状态与审计同事务。锁等待 1.5 秒后报可重试 503。新增发货/修改明细流程必须先锁同一订单，再检查当前状态，避免绕过取消互斥。
- 只有 pending_payment 和演示 paid 可取消；有物流、明细金额不符、预占缺失/数量不符均拒绝。真实 paid 没有退款接入前禁止取消；演示 paid 只记录 simulated，不表示真实资金退回。
- 显式 `scripts/reserve_demo_inventory.py` 补齐可取消演示订单预占并扣库存，重复执行不重复扣减；seed_demo_orders CLI 会调用它。取消只能释放已记录的预占，不能凭历史明细直接加库存。PostgreSQL 详情多查询使用 REPEATABLE READ READ ONLY，确保状态、快照和取消审计来自同一时点。
- 履约与售后由 WorkflowStore 实现：付款 → 发货 → 收货 → 整单退货申请 → 审核 → 寄回 → 验收补库 → 演示退款关闭。一单一个工单，驳回终态，暂不支持部分退款或换货。每单付款/退款/售后唯一；非付款动作与已付款取消必须核对完整付款凭据。发货只将预占转出库，验收补库一次并递增版本。
- 流转按 user → login → order → case → reservations → 排序 products 加锁，业务事件与操作指纹回执同事务保存；网络结果未知保留原操作 ID，禁止切换订单和页内离开，退出登录除外。历史 SFDEMO 订单只在显式脚本中补演示付款/退款和出库凭据，出库记录不重复扣库存。细则见 docs/fulfilment.md。
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

