# OCR 题录纠错检索系统

文献数仓接收 OCR 导出的英文题录，用「错词→正词」替换词典修复 OCR 错词
造成的检索漏检。技术栈：**FastAPI + SQLite FTS5 + 单页网页 + Docker Compose**。

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
返回 DOI 与**原题名**（原题名不改写，改写只发生在索引侧）。

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
| GET | `/` | 操作网页 |
| GET | `/health` | 健康检查 |
| GET | `/api/status` | 活动版本、词典/重建/导入批次状态 |
| POST | `/api/records/import` | 上传题录 CSV（multipart 字段 `file`） |
| POST | `/api/dictionaries` | 上传替换词典，触发后台重建（202） |
| POST | `/api/dictionaries/{id}/retry` | 重试失败/中断的重建 |
| GET | `/api/search?q=...&limit=...` | 全词命中检索 |
| GET | `/api/records?include_superseded=false` | 题录列表（调试/核对） |

## 测试

```bash
.venv/bin/pip install pytest httpx
.venv/bin/python -m pytest tests/ -q                       # 22 个单元/集成测试
.venv/bin/python -I tests/smoke_e2e.py                    # 真实 uvicorn 子进程
                                                          # 端到端（含 kill 重启恢复）
```

覆盖要点：导入四类批次规则、整词/大小写归一、全词命中语义、词典校验、
重建发布、构建期间并发导入与更正的 delta 追赶、并发换词典 409、
故障注入后旧索引不中断、跨进程崩溃重启恢复与重试、孤儿表清理。

## 目录结构

```
app/
  db.py          # 连接/WAL/schema/meta 指针
  normalize.py   # 整词忽略大小写替换
  records.py     # 题录 CSV 导入与批次规则
  rebuild.py     # 词典上传、后台重建、原子发布、崩溃恢复
  search.py      # FTS5 AND 检索（快照一致）
  state.py       # 活动词典内存映射与并发守卫
  main.py        # FastAPI 路由
  static/index.html  # 操作网页
tests/           # pytest 套件 + 真实进程冒烟脚本
Dockerfile, docker-compose.yml
```

## 设计约束说明

- Compose 固定 **单 uvicorn worker**：进程内写锁与重建守卫要求单实例；
  SQLite 单库多写者场景下这也是推荐部署形态。
- 英文词定义为连续 ASCII 字母串（`[A-Za-z]+`），FTS5 使用 `unicode61`
  分词器；数字与字母相邻时按 FTS5 规则属于同一 token。
- 上传文件上限 20 MB。
