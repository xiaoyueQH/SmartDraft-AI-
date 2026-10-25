# SupportFlow

面向电商售后的 Agent 项目，按阶段实现。当前完成基础框架，已进入 **阶段 2：账号权限与业务数据**，其中账号权限已实现。

已有：Vue 3 + TypeScript 用户端/管理端、FastAPI、Pydantic Settings、异步 PostgreSQL、健康检查、请求追踪、有界准入、Alembic 迁移、JWT access/refresh、登录续期、账号管理、改密与标签页账号隔离；管理员选品入库、编辑、上下架、回收站恢复，用户单商品下单与库存预占，本人/后台订单查询、购买快照、两阶段确认取消、库存释放和审计；演示付款、管理员发货、用户收货、整单退货审核、寄回、验收补库、演示退款与工单关闭。

尚未实现：知识检索、LangGraph、换货、部分退款、购物车、真实支付/退款/物流对接。用户端要求登录，管理端及账号接口要求 admin，普通用户页面没有后台入口；商品同时支持演示候选和管理员自己的资料，交易与物流仍为演示环境。商品名称和卡片不重复显示“模拟”标签，页面顶部保留环境说明。

## 本地启动

需要 Conda、Node.js 22.12+、Docker Compose。Python 统一使用独立 Conda 环境 `D:\code\env\supportflow`，不使用 `.venv`。默认端口为 API `8091`、前端 `5183`、PostgreSQL `15432`，与旧项目分开。

在项目根目录创建环境并安装依赖：

```powershell
conda env create --prefix D:\code\env\supportflow --file environment.yml
conda activate D:\code\env\supportflow
Copy-Item .env.example .env
```

修改 `.env` 中的本地数据库密码；本文件由 Git 忽略。启动独立 PostgreSQL：

```powershell
docker compose --env-file .env -f docker/compose.yaml up -d --wait
python scripts/migrate.py
python scripts/create_user.py admin --role admin --display-name 管理员
python scripts/start_api.py
```

另一个终端启动前端：

```powershell
cd frontend
npm ci
npm run dev
```

- 用户端：<http://127.0.0.1:5183/>，未登录时进入登录页。
- 管理端：<http://127.0.0.1:5183/#/admin>，账号管理位于 `#/admin/users`，仅管理员可访问。
- API 文档：<http://127.0.0.1:8091/docs>
- 存活检查：`GET /api/health/live`
- 就绪检查：`GET /api/health/ready`，数据库未就绪时返回 503，不伪报健康。

没有启动 PostgreSQL 也能启动 API 和查看页面，数据服务会显示未就绪。启动脚本可以从其他目录运行；`.env` 始终按项目根定位。前端始终通过 `/api` 代理访问；更改 API 地址时可设置 `SUPPORTFLOW_API_TARGET`。构建后的静态文件部署代理将在部署阶段提供，`npm run preview` 只预览静态页面，不代理 API。

首次创建管理员使用隐藏密码输入，密码至少 12 个字符，没有固定默认账号密码。已有账号无需重复创建；后续更新运行迁移即可，不自动重建表。登录与查询账号需要 PostgreSQL 和迁移就绪。完整双令牌及浏览器多账号说明见 [鉴权设计](docs/authentication.md)。

本地演示可在迁移完成后改用 `python scripts/seed_demo_users.py`，按项目约定创建 `admin / 123456`（管理员）和 `user / 123456`（普通用户）。这是显式运行的演示初始化，不会随服务启动自动创建。密码仍以 Argon2 散列保存；重复执行保留已有账号的密码和角色。正式账号继续使用交互创建命令和至少 12 位密码。

默认数据库连接为 `127.0.0.1:15432`，数据库与数据库用户名均为 `supportflow`，密码见根目录忽略提交的 `.env` 中 `SF_DATABASE__PASSWORD`。账号在 `public.users` 表中；Docker 使用 `supportflow_postgres_data` 命名卷持久化，不是 SQLite 文件。

初始化本地业务演示（先运行迁移和演示账号脚本）：

```powershell
python scripts/migrate.py
python scripts/seed_demo_users.py
python scripts/seed_demo_orders.py
```

脚本为 `user` 创建 3 个演示商品和 5 笔订单，初始覆盖待付款、待发货、运输中、已签收、已取消；随后为可取消演示订单建立库存预占。重复执行不重置已有订单或库存，仅为缺少预占的可取消演示订单扣减相应库存一次。用户端入口为 `#/products`、`#/orders`，后台为 `#/admin/orders`；订单支持状态/完整订单号筛选、物流详情及两阶段取消。新开通的用户没有订单时显示空列表，不会共享演示用户的订单。

表结构、接口与并发边界见 [订单查询说明](docs/orders.md)。

管理员在 `#/admin/catalog` 搜索显卡、笔记本电脑、键盘、耳机、显示器等 24 款虚构候选，确认入库后可编辑名称、说明、售价与库存，也可上下架、移入回收站及恢复。普通用户在 `#/products` 只能看到上架且未删除的商品；选择数量、核对金额后可创建待付款订单，再在 `#/orders` 确认取消并释放库存。不需要淘宝权限或任何外部密钥。已有环境运行迁移并重启 API 即可使用，详见 [商品管理与下单闭环](docs/catalog.md)。

录入自己的商品：点击商品管理右上角“新增商品”，填写 SKU、名称、说明、销售价格和初始库存，确认入库后自动显示在“已入库目录”。SKU 唯一，重复创建会明确提示。新增与候选入库均采用弹窗，打开时不会挤压卡片、改变列数或跳动按钮位置；支持窄屏和 Esc 关闭。请求结果未知时保留原表单和请求，手动重试不会重复创建。

已有演示订单升级取消功能时，运行 `python scripts/migrate.py` 和 `python scripts/reserve_demo_inventory.py`，再重启 API。订单详情中填写原因、检查条件、勾选核对并确认即可取消。已付款演示订单只记录模拟退款，不会向支付渠道发起退款；真实已付款、已发货或已签收订单禁止直接取消。完整规则见 [取消闭环](docs/cancellation.md)。

完整履约与售后：用户在订单详情确认演示付款，管理员登记发货，用户确认收货后可申请整单退货；管理员审核，用户登记退货运单，管理员验收补库后登记演示退款并关闭工单。用户 `#/returns` 与管理员 `#/admin/returns` 分别查看本人/全部工单。付款、退款、工单和事件都持久化，支持原操作 ID 重试，流程图与接口见 [履约与售后闭环](docs/fulfilment.md)。升级到迁移 `0007_order_workflow` 后，显式执行上述预占脚本为历史 SFDEMO 订单补演示付款和出库记录，不重复扣减历史发货库存。

停止本项目数据库：

```powershell
docker compose --env-file .env -f docker/compose.yaml down
```

命名卷保留数据；已初始化后只修改 `.env` 不会更改 PostgreSQL 中的账号密码。

## 结构

```text
backend/
  agents/            Agent 边界，tools/ 与 skills/ 分开，阶段 3 实现
  api/               总路由、公共中间件与错误格式
    router.py        统一挂载 /api
    v1/              第一版接口
  core/              请求追踪
  db/                异步引擎与事务会话
  models/            账号、商品订单 ORM 与查询视图
  services/          鉴权、订单归属与业务编排
  config.py          冻结配置快照
  dependencies.py    HTTP 依赖注入
  main.py            应用工厂与资源生命周期
frontend/src/
  api/               接口访问
  layouts/           用户端与管理端
  router/            路由
  views/             页面
scripts/             启动入口
tests/               配置、事务、生命周期与接口验证
docker/              当前只编排 PostgreSQL
requirements.txt     唯一 Python 依赖清单
```

路由按 `main → api/router.py → v1/ 具体接口` 装配。当前接口地址保持 `/api/...`，`v1/` 仅用于代码组织；新增接口直接在 `api/router.py` 中注册。

PostgreSQL 最大连接数为每 API 进程 `pool_size + max_overflow`，默认 5 + 5，共 10 个。取连接默认最多等 3 秒，建立连接 3 秒，执行 SQL 5 秒；慢查询和闲置事务有服务端超时保护。增加 API 进程前需要计算整个部署的连接总预算。

HTTP 每进程默认最多 64 个请求执行，等待 0.25 秒仍无名额返回 503 / `SERVER_BUSY` 和 `Retry-After`。异常与取消都会释放名额，存活检查不受此名额限制。该准入只覆盖 HTTP；未来接入 WebSocket 时需按每轮业务请求控制，空闲连接不占模型名额。模型/工具还需在真实调用入口设置独立并发预算。

事务由 `Database.session()` 管理；不在整个流式对话期间持有连接。后续将按“读取业务事实 → 调用模型/外部工具 → 短事务校验并保存”的顺序组织业务。确认动作需要重新校验订单状态。

## 验证

```powershell
conda activate D:\code\env\supportflow
python -m pytest -q
python -m ruff check backend scripts tests
python -m ruff format --check backend scripts tests
cd frontend
npm test
npm run format:check
npm run build
```

SQLite 驱动仅用于不依赖 PostgreSQL 的事务测试；生产配置只接受 PostgreSQL + asyncpg。SQLite 测试不能代替真实 PostgreSQL 连接池、锁和业务并发验收。

本地 PostgreSQL 启动后，可显式启用集成验收：

```powershell
$env:SUPPORTFLOW_TEST_POSTGRES = "1"
python -m pytest -q
Remove-Item Env:SUPPORTFLOW_TEST_POSTGRES
```

集成测试使用独立的随机 `acceptance_*` schema，验证 32 个并发写事务、请求取消回滚、连接池耗尽超时及慢查询后连接恢复；结束时清理自己的测试 schema。仅对本项目的本地测试库执行。这些是基础设施验收，不代表真实 Agent 业务的吞吐能力。

完整履约、售后、Chrome 双账号及 24/64 并发 HTTP 读取结果见 [业务验收报告](docs/verification.md)。可运行 `python scripts/check_business_load.py --users admin user --concurrency 24 --requests 240` 复跑本机只读业务基线，密码交互输入；不包含 Agent 或真实支付渠道的性能。

`environment.yml` 只定义 Conda 的 Python/pip 与 `requirements.txt` 引用，不重复维护 Python 包版本。更新依赖后，在激活的环境中运行 `python -m pip install -r requirements.txt`。不修改已有 `medical_rag` 环境。

## 后续阶段

1. **售后政策**：商品、下单、取消、履约和整单退货退款演示闭环已完成，下一步维护有版本的售后政策及边界案例，供后续 Agent 查询。
2. **售后 Agent**：业务稳定后接入单 Agent + LangGraph，复用订单查询、取消预览等业务工具，写入保持独立的用户确认；同步提供真实执行时间线、结果摘要、耗时和失败恢复。
3. **闭环与验收**：工单状态机、人工接管、后台任务、外部超时兜底和真实 Agent 并发测试。

数据来源将区分公开数据、中文适配与自行生成；独立保存评估案例。不以数据行数充当业务完成度。

Agent 操作白盒设计已记录在 [docs/agent-execution.md](docs/agent-execution.md)，包括用户/客服可见范围、结构化事件、确认机制、短事务保存、SSE 续读与并发边界。目前是设计，尚未实现运行中的 Agent 或执行时间线，不展示虚构执行结果。
