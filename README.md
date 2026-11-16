# OCR 题录纠错检索系统

文献数仓接收 OCR 导出的英文题录，用「错词→正词」替换词典修复 OCR 错词
造成的检索漏检；同时支持文献数据员把**人工校正的题名/摘要**交给下游：
以保存校订时所见 OCR 为基底，新 OCR 修订入库时逐字段三方合并，冲突整条
暂停供数并保留三方内容供人工裁决。技术栈：**FastAPI + SQLite FTS5 +
单页网页 + Docker Compose**。

## 快速启动

```bash
docker compose up --build
# 浏览器打开 http://localhost:8000
```

数据持久化在命名卷 `ocr_lit_data`（容器内 `/data/app.db`）。

本地开发（无 Docker 时）：

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
DB_PATH=data/app.db .venv/bin/uvicorn app.main:app --port 8000
```

## 文件格式

**题录 CSV**（UTF-8，首行表头；`abstract` 可省略，可只给前三列）：

```csv
doi,revision,title,abstract
10.1000/abc,1,Teh Quantum Optics,studis of photon behavior
```

**替换词典 CSV**（也支持无表头的纯两列文件）：

```csv
wrong,correct
teh,the
machne,machine
```

- 按**完整英文词**匹配、**忽略大小写**替换；非字母字符原样保留，
  因此 `Tehniques` 不会被错词 `teh` 部分替换。
- 正词保留词典中的原始写法；每个词只查表一次，不存在链式替换。
- 正词不得再作为错词出现（上传时集合互斥校验，冲突整文件拒绝）。
- 错词重复、含非字母字符的词条同样拒绝。

## 业务规则

### 题录导入（整批事务，全收或全退）

| 情形 | 处理 |
|---|---|
| 修订号高于现行版本 | 采纳更正，旧文标记 `superseded` 并移出检索 |
| 同修订号 + 同题名/摘要（哈希一致） | 复用，不重复写入 |
| 同修订号但内容不同 | **整批拒收**（400，批次留痕） |
| 修订号低于现行 | **整批拒收** |
| 批内同一 DOI 重复 | **整批拒收** |

### 检索

查询词与题录走同一套归一规则；FTS5 以 AND 语义要求**全部查询词均命中**，
返回 DOI 与**原题名**（原题名不改写，改写只发生在索引侧）。人工校订不
改变原始题录与检索行为，检索始终基于 OCR 原文与活动词典。

### 人工校订与三方合并（供数资格）

人工版以**保存校订时所见的 OCR 现行记录**为基底（B，base），与后续的
现行 OCR（O）、人工版（H）对题名、摘要**逐字段独立合并**：

| O 相对 B | H 相对 B | 处理 | 字段来源 |
|---|---|---|---|
| 未改 | 未改 | 取 B | `base` |
| 改 | 未改 | 自动取 O | `ocr` |
| 未改 | 改 | 自动取 H | `human` |
| 改，O==H | 改，O==H | 双方改成相同值，直接采用 | `ocr` |
| 改，O!=H | 改，O!=H | **字段冲突**，整条 DOI 暂停供数 | — |

- 可自动合并的修订在 OCR 导入事务内**立即更新供数**；任一字段冲突则整条
  记录 `status=conflict`，合并值不落库，base/ocr/human 三方内容完整保留，
  网页「裁决冲突」对话框逐字段三选一（只接受三方既有内容）。
- 裁决绑定页面所见的 OCR 行 ID 与人工版 ID；之后 OCR 修订或人工版任一
  变化都会触发重新合并并作废旧裁决，旧页面提交得到 **409**，旧页面无法
  恢复供数，必须刷新后重新处理。
- 重新保存人工版会以当前现行 OCR 为新基底生成新版本（旧版置 `superseded`
  保留审计），并立即重新合并。
- **同生共死**：OCR 批次接纳、校订状态变化、供数资格更新在同一个写事务
  内提交，写入失败整批回滚、校订与供数保持原状；人工校订提交本身也是
  整批事务（全收或全退，拒收留痕）。
- 未校订题录没有校订状态行，供数时**沿用最新 OCR**（来源标 `ocr`）。
- 全部校订、冲突、裁决均落盘，**重启后完整保留**，无需任何人工干预。

### 供数 CSV 下载（`GET /api/export/supply.csv`）

- 只含**可供数**题录：无冲突的未校订/已校订现行记录；冲突题录整条排除。
- 整个导出由**单条只读 SQL** 生成，所有行来自同一已提交快照。
- 列：`doi, revision, title, abstract, title_source, abstract_source,
  ocr_base_record_id, ocr_live_record_id, human_correction_id`；
  `source ∈ base/ocr/human/manual`（manual=冲突人工裁决），未校订行的
  `human_correction_id` 为空、`ocr_base_record_id` 即现行 OCR 行。

### 词典换版与索引重建（核心一致性设计）

1. 上传校验通过后创建**新的空 FTS 表**并登记 `dict_versions(status=building)`；
   旧 FTS 表与旧词典在整个构建期间继续服务检索，题录导入照常写入当前索引，
   已接纳的更正立即影响当前检索。
2. 后台线程以短事务分批把现行记录按新词典灌入新表，不阻塞写入。
3. 发布在**单个写事务**内完成：先做 delta 对账（补齐构建期间新导入/更正的
   记录、移除被取代的旧行），再原子切换 `meta.active_fts` 与
   `meta.active_dict` 指针；随后内存映射切换、旧表删除。
   → 新索引必然包含构建期间全部已提交修订，且半成品永不对外可见。
4. 一次查询的词典映射、FTS 命中与题录读取处于同一只读快照（单条 SQL 语句
   的一致性视图），不会读到换版中途的混合状态。
5. 同一当前版并发换词典：进程内锁 + 数据库 `building` 状态双重守卫，
   第二个上传得到 409，只有一份能发布；发布时再校验基线 FTS，
   跨进程/重启场景下的抢跑版本会被标记 `abandoned`。
6. 构建失败或进程被 kill：活动指针不动，旧索引继续可用；重启后中断任务
   标记为 `failed`、孤儿 FTS 表自动清理，可在网页或 API 上**重试**。
   极端情况下活动 FTS 丢失也会在启动时按活动词典自动重建。

## HTTP API

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/` | 操作网页（检索、人工校订、冲突裁决、供数下载、导入与重建） |
| GET | `/health` | 健康检查 |
| GET | `/api/status` | 活动版本、词典/重建/导入批次、校订统计与校订批次状态 |
| POST | `/api/records/import` | 上传题录 CSV（multipart 字段 `file`），已校订 DOI 同事务重合并 |
| POST | `/api/dictionaries` | 上传替换词典，触发后台重建（202） |
| POST | `/api/dictionaries/{id}/retry` | 重试失败/中断的重建 |
| GET | `/api/search?q=...&limit=...` | 全词命中检索（原始题录，不受校订影响） |
| GET | `/api/records?include_superseded=false` | OCR 题录列表（调试/核对） |
| POST | `/api/corrections` | 提交人工校订批次 `{"items":[{doi,title,abstract}]}`（整批事务） |
| GET | `/api/corrections?status=clean\|conflict` | 校订状态列表（含三方内容与来源） |
| GET | `/api/corrections/{doi}` | 单 DOI 校订详情（冲突时含 base/ocr/human 全文） |
| POST | `/api/corrections/{doi}/resolve` | 冲突裁决（带版本绑定，过期返回 409） |
| GET | `/api/export/supply.csv` | 供数 CSV 下载（同一快照，仅可供数题录） |

## 测试

```bash
.venv/bin/pip install pytest httpx
.venv/bin/python -m pytest tests/ -q                       # 42 个单元/集成测试
.venv/bin/python -I tests/smoke_e2e.py                    # 真实 uvicorn 子进程
                                                          # 端到端（含 kill 重启恢复、
                                                          # 校订三方合并与重启持久化）
```

覆盖要点：导入四类批次规则、整词/大小写归一、全词命中语义、词典校验、
重建发布、构建期间并发导入与更正的 delta 追赶、并发换词典 409、
故障注入后旧索引不中断、跨进程崩溃重启恢复与重试、孤儿表清理；
人工校订三方合并矩阵（单方改变/同值/分歧）、冲突整条暂停与三方保留、
版本绑定裁决与 409 旧页面失效、整批原子性（提交与导入回滚）、
重启后校订/冲突/供数资格持久化、供数 CSV 快照一致性、检索与词典能力并存。

## 目录结构

```
app/
  db.py          # 连接/WAL/schema/meta 指针（含校订三张表）
  normalize.py   # 整词忽略大小写替换
  records.py     # 题录 CSV 导入与批次规则（事务内触发校订重合并）
  corrections.py # 人工校订、三方合并、冲突裁决、供数 CSV 快照
  rebuild.py     # 词典上传、后台重建、原子发布、崩溃恢复
  search.py      # FTS5 AND 检索（快照一致）
  state.py       # 活动词典内存映射与并发守卫
  main.py        # FastAPI 路由
  static/index.html  # 操作网页（含校订/裁决/下载界面）
tests/           # pytest 套件 + 真实进程冒烟脚本
Dockerfile, docker-compose.yml
```

## 设计约束说明

- Compose 固定 **单 uvicorn worker**：进程内写锁与重建守卫要求单实例；
  SQLite 单库多写者场景下这也是推荐部署形态。
- 英文词定义为连续 ASCII 字母串（`[A-Za-z]+`），FTS5 使用 `unicode61`
  分词器；数字与字母相邻时按 FTS5 规则属于同一 token。
- 上传文件上限 20 MB。
