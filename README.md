# SupportFlow

面向软件开发团队的需求与售后协作 Agent。客户先与业务顾问沟通，逐渐明确业务与问题；需求草案由客户与负责人确认后才正式立项并建立基线，后续新增、修改、删除功能通过变更单处理，故障和使用咨询通过工单跟进。

## 当前可用流程

- 客户直接点击“开始需求沟通”，不用填写项目名称、完整说明或技术栈。管理员为指定客户发起沟通；普通账号没有后台入口。
- 顾问结合上下文自然解释、给建议和比较取舍，必要时再问关键问题。普通沟通默认一次模型调用，新增事实通过工具记录并核对来源；纯解释可直接回答。
- 沟通阶段phase=discovery，仅显示对话、已了解的信息和需求草案。仅客户明确要求时调用独立起草服务；起草前先保存沟通，失败保留原话与已核实信息。双方确认发布V1后才转为正式项目，开放交付与售后模块。
- 需求访谈为主顾问加独立起草子Agent；正式项目使用业务Agent原生工具循环，读取本项目资料、对比基线并准备需求/变更/故障/咨询草稿；负责人还可准备任务与交付记录。
- 需求与变更：草稿 → 提交 → 客户与负责人分别确认 → 发布新版基线。待确认问题未清空不能发布，修改草稿清除既有签名，已发布原文不可覆盖。变更正文须包含变更说明和变更后完整需求书。
- 售后、任务与验收：草稿 → 提交 → 负责人处理 → 提交结果与依据 → 客户验收关闭；未通过可退回继续处理。处理说明和历史操作保留。
- 处理过程中可追加补充说明；需求变更提供旧基线与当前正文并排核对，发布新版本后仍保留旧版。
- 页面“管理记录”可搜索、重命名、单条或批量移入回收站、恢复记录。客户管理本人沟通记录，正式项目由管理员管理；对话、文档与操作历史保留。正在执行Agent的记录须等执行结束再删除。
- 管理后台使用独立总览及跨项目的客户、需求审核、开发任务、交付验收、售后工单列表；支持客户/项目/状态筛选，点击具体记录后进入对应项目与处理弹窗。管理员登录进入/admin，普通客户不显示管理菜单。
- 需求书提供 Markdown 预览、导出和 Mermaid 流程图；模型产生的 HTML 经清洗，不加载文档中的远程图片。
- 记录实际模型、工具事件、耗时与用量；客户只见易懂步骤和耗时，不显示Token、原始参数或私有推理。工具只能准备草稿，不能替双方确认或宣称完成开发。
- 沟通窗口历史独立滚动，底部输入；每轮回复中的“正在思考/已完成”可展开实际调用与耗时，记录随该轮消息保存。右侧保留业务纪要；不显示空执行面板。

本版资料入口为自然语言沟通、累计纪要与手动文档；尚未实现附件上传解析、语义向量检索、GitHub/Jira 对接、代码生成执行、自动报价或自动排期。Agent 未配置时明确提示，手动流程可继续，不以模板冒充模型。

需求沟通节点、人工断点和联网控制的完整协议见 [需求沟通流程与 HITL](docs/requirement-consultation.md)。

## 本地启动

使用 Conda Python 3.12、Node.js 22.12+、Docker Compose。当前本机环境 `D:\code\env\supportflow`；依赖统一维护根 requirements.txt。

```powershell
conda activate D:\code\env\supportflow
python -m pip install -r requirements.txt
# 首次部署复制 .env.example 为 .env，已有环境保留原文件和数据库密码。
docker compose --env-file .env -f docker/compose.yaml up -d --wait
python scripts/migrate.py
python scripts/create_user.py admin --role admin --display-name 管理员
python scripts/start_api.py
```

另一个终端：

```powershell
cd frontend
npm ci
npm run dev
```

- 客户工作台：http://127.0.0.1:5183/#/
- 负责人工作台：http://127.0.0.1:5183/#/admin
- 账号管理：#/admin/users；运行状态：#/admin/status。
- API 文档：http://127.0.0.1:8091/docs
- PostgreSQL：127.0.0.1:15432，数据库/用户 supportflow，密码保存在被忽略的 .env；账号位于 public.users。
- Redis：127.0.0.1:16379，只缓存执行快照与后台汇总；配置、权限和故障回源见[缓存说明](docs/cache.md)。
- 首次使用scripts/create_user.py交互创建管理员，再由管理员开通客户。现有账号保留；启动不会初始化示例账号或项目。

## 模型配置

在被忽略的根 .env 配置 OpenAI 兼容的 Chat Completions 服务，要求支持原生 tool_calls：

```dotenv
SF_AGENT__BASE_URL=https://dashscope.aliyuncs.com/compatible-mode/v1
SF_AGENT__MODEL=qwen-max
SF_AGENT__API_KEY=在本机填写
SF_AGENT__CAPACITY=4
SF_AGENT__TIMEOUT_SECONDS=30
SF_AGENT__RUN_SECONDS=90
SF_AGENT__TOKEN_BUDGET=40000
```

修改后重启 API。密钥不出现在页面、日志或 Git 中。默认每进程4个Agent执行名额；正式项目每轮最多5次模型调用、8次工具调用、3份草稿；独立起草共享8次模型/8次处理工具/64000token/150秒，另允许已有章节的一次本地收尾，并限制上下文与累计模型用量。请求前按UTF-8字节保守预留输入预算并限制输出，响应按总用量或输入/输出之和计量；供应商不返回用量时按预留上限计量，正式项目超额本轮失败且不保存草稿；起草子Agent保留已写章节并标为部分完成。模型/工具期间不持有数据库事务。跨进程以 PostgreSQL 项目行锁和运行记录唯一索引互斥；HTTP请求立即返回执行ID，刷新后可继续查询，不会重复执行。后台任务随API进程运行，崩溃后超过180秒的记录在读取或新请求时标记失败，需人工重新发送；尚无跨进程任务队列。

## 结构

```text
backend/
  agents/orchestrator.py          单 Agent 决策 / 工具循环与执行预算
  agents/consultation/graph.py    流程连接与HITL恢复
  agents/consultation/nodes.py    业务沟通、按需起草决策与人工断点
  agents/drafting/agent.py       独立起草子Agent与原生工具循环
  agents/drafting/state.py       每次执行独立的Pydantic状态
  agents/tools/                  有权限边界的项目工具和公开搜索
  api/router.py                   总路由
  api/v1/projects.py              项目、需求、工单、执行记录接口
  db/session.py                   AsyncEngine → async_sessionmaker → AsyncSession
  db/project_store.py             项目事务、版本、确认、幂等和执行保存
  db/cache.py                     异步Redis读取缓存和受控回源
  db/migrations/versions/0011_*    沟通阶段和累计纪要迁移（保留历史数据）
  models/projects.py              项目数据与命令校验
frontend/src/
  views/ProjectsView.vue           客户/负责人项目工作台
  components/RequirementDocument.vue  安全文档与流程图预览
  api/projects.ts                 业务类型
```

JWT access/refresh、每标签页账号隔离、短事务、有界HTTP准入、数据库连接池继续沿用。项目所有读取/写入校验本人归属或管理员身份；工单操作使用版本号与请求ID防止覆盖和重复提交。客户先进行业务访谈，提出整理后形成需求草案，补齐问题并经双方确认后立项；后续通过变更和售后记录跟进。

## 验证

本轮浏览器实测、发现的问题、自动检查和审查结果见[需求沟通与起草验收](docs/acceptance.md)。

改动过程中仅运行相关测试，收尾集中验证一次。PostgreSQL测试使用隔离的临时schema并自行清理，不修改业务数据：

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

旧电商运行代码、页面、接口、测试、初始化脚本与重复文档已移除。历史数据库表与迁移链保留，避免破坏已有数据。删除前的70个文件保存在本机忽略提交的 `storage/cleanup-archive/commerce-before-removal-20261025.zip`，不参与运行。客户已有沟通与项目记录未删除。

主沟通将已存纪要和原话交给独立起草子Agent。它通过原生工具自主选择规划、搜索、写章、查看正文或结束，也可直接生成；所有工具共享本次Pydantic状态，失败保留已有内容。详见[Draft Agent流程](docs/draft-agent.md)。
