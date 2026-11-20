# 业务窗关账

> 遵守 `ops/底座生成前必读_效率与技术栈.md`（**≥3× 厚度**）。  
> FastAPI + Vue 3 + SQLite；交付须 `docker compose up -d --build` 一键起。  
> **禁止同构换皮**：本仓主链路、表结构、接口与页面均独立。

## 骨架一句话

事件按业务窗裁切，迟到进旁路或拒收，关窗后物化结果并与窗口对账。

## 基本信息

| 项 | 值 |
|----|-----|
| 产品名（首页） | 业务窗关账 |
| 目录 | `08-windowcut` |
| 接口 | http://127.0.0.1:11019 |
| 库 | `data/app.db` |
| 账号 | admin / 123456 |

## 厚度（相对薄壳 ≥3×）

| 项 | 目标 |
|----|------|
| 业务 LOC | ≥900（目标 1000+） |
| 后端具名模块 | `db` / `auth` / `locks` / `windows_svc` / `ingest_svc` / `late_svc` / `materialize_svc` / `reconcile_svc` / `audit_svc` / `hold_svc` / `reopen_svc` |
| 前端视图 | 业务窗 / 入账 / 迟到桶 / 物化 / 对账 / 审计争用 |
| 空桩 | hold 晋升、迟到补入、分区物化、窗重开 |

## 为何能出多题

开窗关窗、迟到策略、窗重开、部分物化失败回滚、旁路暂存 vs 拒收、争用锁与审计。

## 互斥切口候选

- 迟到拒收 vs 进迟到桶
- 物化失败整窗回滚 vs 保留成功分区
- 窗重开作废重算 vs 增量补丁合并
- 切版旁路暂存 vs 开始瞬间拒收

## 世代对账口径（钉死，当天禁止改口）

本仓对账正确性只走 **甲路径：世代过滤查询**。

- 对账只统计窗口 **当前 generation** 的窗内事件；
- 物化行写入时钉住当时 generation，对账只认当前世代行，**跨代行整笔排除**；
- 乙路径 `reconcile_snap` 快照复检不实现（`backend/reconcile_snap.py` 调用即抛错），两条路径不得混用；
- 口径登记在库表 `generation_ledger`（按日期一行，当天已登记则不覆盖）；
- 代码路径与对账册登记不一致时，对账接口拒写（409）并记审计 `reconcile_deviation`（原因「对账偏了」）；
- 同一 `window_id` 对账须占锁 `reconcile:{wid}`，按会话区分持有者，后到只记争用（409，不返回对账结果）。

对齐判定 = 当前世代物化行为「成功」且 `row_count`/`total_amount` 与当前世代窗内事件合计一致。
对账页、窗口世代、物化台账、事件列表、审计五处均展示同一世代口径。

## 启动（一键）

```powershell
cd projects/08-windowcut
docker compose up -d --build
```

浏览器：http://127.0.0.1:11019/  
健康检查：GET http://127.0.0.1:11019/api/health

本机开发：

```powershell
python -m pip install -r requirements.txt
python -m uvicorn app:app --app-dir backend --host 127.0.0.1 --port 10605
python -m pytest -q
```
