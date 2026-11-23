# 熔炉工坊 · 实时数据融合与宽表构建平台

面向多源异构数据的实时接入融合、宽表映射构建、维度关联编排、数据质量门禁与分区快照管理的实时数据融合平台。

这是一个前后端分离的管理平台：前端 Vue 3 + Vite + TypeScript，后端 FastAPI（Python）。
两边各自独立启动，前端 dev server 已关掉自动打开页面，启动后按终端打印的地址手工打开。

## 目录结构

```text
.
├── frontend/                 Vue 3 + Vite + TypeScript 前端
│   ├── src/views/WorkbenchView.vue|tsx  全部模块共享工作台
│   ├── src/data/modules.ts              18个业务模块配置
│   ├── src/api/              统一请求封装
│   └── vite.config.ts        dev server 配置（open: false）
├── backend/                  FastAPI（Python） 后端
│   ├── app/routers/resource.py  通用查询/导出/登记/动作接口
│   ├── app/services/<key>.py    每个模块独立领域逻辑
│   ├── app/services/base.py     共享服务骨架
│   └── app/store.py          内存数据仓库与示例数据
├── .gitignore
└── docker-compose.yml
```

## 启动

### 后端

```bash
cd backend
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
./run.sh
```

健康检查：`curl http://127.0.0.1:8000/api/health`

### 前端

```bash
cd frontend
npm ci
npm run dev
```

前端默认监听 `http://127.0.0.1:5173/`，dev server 不会自动打开浏览器，
需要自己访问。`/api` 由 vite 代理到后端 `http://127.0.0.1:8000`。

## 业务模块

| 模块 | 目录 | 业务对象 | 主要字段 |
| --- | --- | --- | --- |
| 数据源定义 | `source_def` | 数据源 | 源编号、源名称、源类型 |
| Schema映射 | `schema_map` | 映射方案 | 方案编号、源表名称、目标宽表 |
| 维度表 | `dim_table` | 维度定义 | 维度编号、维度名称、主键字段 |
| 融合作业 | `fusion_job` | 融合任务 | 作业编号、目标宽表、参与数据源 |
| 冲突仲裁 | `merge_conflict` | 冲突规则 | 规则编号、目标宽表、冲突字段 |
| 质量门禁 | `quality_gate` | 门禁规则 | 规则编号、目标宽表、检查类型 |
| 分区快照 | `snapshot_mgr` | 快照分区 | 分区编号、目标宽表、分区键值 |
| 分区放行 | `partition_gate` | 分区放行 | 放行编号、融合批次、分区编号、确认结果、关联凭据 |
| 数据回填 | `backfill_task` | 回填任务 | 任务编号、目标宽表、回填区间 |
| 数据血缘 | `lineage_edge` | 血缘边 | 边编号、上游节点、下游节点 |
| 重放队列 | `replay_queue` | 重放记录 | 记录编号、融合作业、失败原因 |
| 水位追踪 | `watermark_track` | 水位记录 | 记录编号、数据源、当前水位 |
| 分区策略 | `partition_rule` | 分区定义 | 策略编号、目标宽表、分区类型 |
| 宽表定义 | `wide_table_def` | 宽表元数据 | 表编号、宽表名称、字段列表 |
| 增量归并 | `delta_merge` | 增量批次 | 批次编号、融合作业、归并键 |
| 冷热分层 | `cold_store` | 分层策略 | 策略编号、目标宽表、热数据天数 |
| 快照导出 | `export_snapshot` | 导出计划 | 计划编号、目标宽表、导出格式 |
| 指标聚合 | `metric_aggr` | 聚合规则 | 规则编号、目标宽表、聚合函数 |
| 交叉校验 | `cross_validate` | 校验方案 | 方案编号、主体宽表、参照宽表 |
| 数据漂移 | `data_drift` | 漂移记录 | 记录编号、宽表名称、漂移类型 |
| 融合监控 | `fusion_monitor` | 监控指标 | 指标编号、融合作业、监控维度 |
| 变更审计 | `audit_trail` | 审计记录 | 记录编号、变更目标、操作人 |
| 目录检索 | `catalog_search` | 数据目录 | 目录编号、宽表名称、标签列表 |

## 约定

- 全部模块共享`frontend/src/views/WorkbenchView.vue|tsx`和一套资源路由。
- 每个模块仍保留独立`backend/app/services/<模块>.py`；纯前端保留
  `frontend/src/domain/<模块>.ts`，用于承载真实业务差异并维持36题位容量。
- 列表接口统一返回 `{ items, total, page, size }`，动作接口统一返回 `{ ok, message }`。
- 状态流转只允许在 `app/services` 里改，路由层不做业务判断。

## 质量门禁分区放行与跨日补货凭据

门禁规则按级别逐分区校验，核心状态机在 `backend/app/partition_gate.py`：

- **规则级别**：阻断级（`block`/`阻断` 等）触发才置「待核」；告警级（如行数低于阈值）
  只记录不阻断，告警随实际检查覆盖的分区版本（`partition_version`）走。
- **版本化结果**：同一 `(分区, 版本)` 重跑覆盖旧结果，检查行数不重复累计。
- **发布保护**：待核分区拒绝用本次数据替换已发布内容；放行分区发布后保留版本号。
- **取数凭据**：每个夜班补货请求只确认一次凭据（`/api/partition-gate/credentials`）；
  批次含待核分区时不发本期凭据，回退上个已确认窗口合计，并可回查等待原因
  （`/api/partition-gate/wait-reasons/{request_id}`），随后按凭据日期和金额下单。
- **签出核对**：`/api/partition-gate/checkouts/{batch_code}` 核对实际签出日期，
  待核分区即使被签出也取消本期资格，不能冒充本期新数据。
- **可读明细**：`/api/partition-gate/releases/{batch_code}` 逐分区注明确认结果、
  阻断/告警规则与关联凭据编号。

场景测试见 `backend/tests/test_partition_gate.py`，路由层手工冒烟脚本见
`backend/scripts/smoke_partition_gate.py`。

领域：数据工程
子方向：数仓与数据湖
