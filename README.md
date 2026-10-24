# SupportFlow

面向电商售后的 Agent 项目，按阶段实现。当前完成基础框架，已进入 **阶段 2：账号权限与业务数据**，其中账号权限已实现。

已有：Vue 3 + TypeScript 用户端/管理端、FastAPI、Pydantic Settings、异步 PostgreSQL、健康检查、请求追踪、有界准入、Alembic 迁移、JWT access/refresh、登录续期、账号管理、改密与标签页账号隔离。

尚未实现：商品订单、知识检索、LangGraph、售后工单、支付/物流对接。首页标记“即将提供”的内容是规划，不是可操作业务。用户端要求登录，管理端及账号接口要求 admin，普通用户页面没有后台入口。

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
  models/            阶段 2 加入实际模型
  services/          阶段 2 加入实际业务
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

`environment.yml` 只定义 Conda 的 Python/pip 与 `requirements.txt` 引用，不重复维护 Python 包版本。更新依赖后，在激活的环境中运行 `python -m pip install -r requirements.txt`。不修改已有 `medical_rag` 环境。

## 后续阶段

1. **业务数据**：账号权限和迁移已完成；继续实现商品、订单、库存、物流模拟数据及业务数据归属校验。
2. **售后 Agent**：单 Agent + LangGraph、按业务划分的工具、政策 RAG；先实现取消订单、退换货、物流异常三条业务，写操作确认。同步实现真实执行时间线，查看步骤、工具输入/输出摘要、耗时、引用及失败恢复。
3. **闭环与验收**：工单状态机、人工接管、幂等、后台任务、外部超时兜底和真实 HTTP 并发测试。

数据来源将区分公开数据、中文适配与自行生成；独立保存评估案例。不以数据行数充当业务完成度。

Agent 操作白盒设计已记录在 [docs/agent-execution.md](docs/agent-execution.md)，包括用户/客服可见范围、结构化事件、确认机制、短事务保存、SSE 续读与并发边界。目前是设计，尚未实现运行中的 Agent 或执行时间线，不展示虚构执行结果。
