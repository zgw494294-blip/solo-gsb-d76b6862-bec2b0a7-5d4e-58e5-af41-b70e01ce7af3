# 共炉保温编排台

本地单页应用：为 2～3 种需要在同一台炉子里分别完成连续保温的材料，
编排一条满足全部约束的**最短炉温设定曲线**；长度相同取温度序列
**字典序最小**的方案。

支持**执行记录续炉判定**：可输入 1～20 个已经执行的 5 分钟炉温档位，
逐档核验合法后，以最后已执行温度接续，在 20 档总上限内编排最短且回到
20°C 的后续曲线；首个违规档即拒绝续排。

- 后端：Python + FastAPI（求解核心见 `app/solver.py`，纯标准库 BFS；
  配方 / 炉次 / 归档用 SQLite 持久化，见 `app/db.py`、`app/store.py`；
  已归档炉次只读电费试算见 `app/electricity.py`，原曲线复用只读核验见
  `app/reuse.py`）
- 前端：原生 HTML / CSS / JavaScript（`static/`，无任何前端构建与框架，
  页面分为「编排求解 / 材料配方 / 炉次草稿」三个标签页；已归档炉次详情中
  含原曲线复用检查与电费试算卡片）

## 一条命令启动

需要 Docker（含 Compose 插件）。

```bash
docker compose up --build -d
```

启动后浏览器访问：

- 页面：<http://localhost:8000/>
- 健康检查：<http://localhost:8000/healthz>
- 接口文档（FastAPI 自带）：<http://localhost:8000/docs>

停止：

```bash
docker compose down
```

端口在 `docker-compose.yml` 的 `ports` 中修改（宿主端口在冒号左侧）。

### 不用 Docker（可选）

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
uvicorn app.main:app --host 0.0.0.0 --port 8000
```

## 业务规则

### 材料（2～3 种）

- 保温下限 / 上限：40～180°C 的整十数，下限不高于上限；
- 连续保温时长：5、10 或 15 分钟（对应 1、2、3 个连续档位）；
- 耐受上限：不低于保温上限、至多 200°C 的整十数。

### 炉温曲线

- 炉温初态 20°C；
- 每档持续 5 分钟，设定温度为 20～200°C 的整十数；
- 首档与初态温差、相邻档温差均不超过 20°C；
- 末档必须回到 20°C；
- 所有档位温度都不得超过**任何**材料的耐受上限；
- 只有连续达到所需时长（5 分钟一档）的档位全部落在该材料的保温区间
  `[下限, 上限]` 内，该材料才算达标；
- 不同材料可以共用同一档位；材料一旦达标，后续档位对它不再有要求。

### 目标

- 总档数 ≤ 20 的前提下，求档数最少的完整曲线；
- 最短解有多条时，取温度序列字典序最小（例如先升 40°C 排在先升 60°C
  之前）；
- 有解：展示曲线图、各材料**首次达标**的连续档位区间、逐档明细表，
  以及被多种材料共用的档位；
- 无解：页面给出明确的中文原因，**不展示任何部分方案**；
- 修改任意输入后，旧结果立即清除。

### 执行记录续炉判定（可选）

- 在「已执行档位」中输入 **1～20 个**已经执行的 5 分钟炉温档位
  （20～200°C 的整十数，逗号/空格分隔）；HTTP 请求中对应
  `executedTemperatures` 字段（数组，可省略）。
- 从初态 20°C 起，按下列规则**逐档**核验已执行记录：
  - 温度必须是 20～200°C 的整十数；
  - 首档与初态、相邻档温差均不超过 20°C；
  - 每档都不得超过任何材料的耐受上限；
  - **一旦违规，立即指出首个违规档位（1-based）及原因，不给出任何续排**
    （HTTP 422，`detail` 为中文说明）。
- 合法记录逐档累计各材料的**连续**保温进度：
  - 档位落在保温区间内则进度 +1，达到所需档数即达标；
  - 档位离开区间时，**未达标者进度归零**，**已达标者保持达标**；
  - 记录中途回到 20°C 不影响合法性，后续仍可继续编排。
- 续排规则：
  - 以**最后一个已执行温度**为起点，已用档数**计入 20 档总上限**；
  - 求总档数最短、末档回到 20°C 的后续曲线；等长时取后续温度序列
    字典序最小；
  - 各材料**首次达标**区间按完整曲线（已执行 + 后续）统一编号，允许
    横跨已执行与后续档位；
  - 若已全部达标且末档已在 20°C，后续曲线为**空**；
  - 若总档数 20 以内无可行后续，返回 `solvable:false` 并明确提示，
    不展示部分曲线。

## 共享材料配方与炉次草稿（SQLite 持久化）

除了上面的即时编排，服务还支持把**材料参数保存为配方**，让多个**炉次草稿**
按顺序共用 2～3 个现有配方；核准的炉次方案可以**归档冻结**。数据保存在
SQLite（默认 `/data/planner.db`），Compose 以命名卷 `planner-data` 挂载，
容器重启后仍可读取。

### 材料配方 `/api/recipes`

- 字段与即时编排的材料**完全一致并复用同一套校验**：
  `name`（可留空，≤20 字）、`holdLow` / `holdHigh`（40～180°C 整十数，
  下限不高于上限）、`duration`（5/10/15 分钟）、`tolerance`（不低于保温
  上限、至多 200°C 的整十数）。
- 支持**创建、读取、整份修改**（PUT 为全量替换）。配方创建时版本为 1，
  每次成功修改版本 +1；**非法修改返回 422，不写入任何数据**（配方版本、
  引用草稿均保持原样）。
- 每个配方带有**状态**：`active`（可用，默认）/ `disabled`（停用）。
  新建配方与旧数据库中已有的配方一律为可用；状态保存在 SQLite 中，
  重启后保留。`GET /api/recipes` 始终列出**全部**配方并附 `status`。
- **停用 / 恢复**：`POST /api/recipes/{id}/disable`、
  `POST /api/recipes/{id}/enable`（未知配方返回 404；重复停用 / 恢复
  幂等返回 200）。状态切换**不改变材料参数、不增加配方或草稿版本、
  不重算任何炉次**；停用的配方仍可整份修改（保持停用），原有引用草稿
  照常重算与归档，已归档方案不受影响。恢复后可再次被新炉次引用。

### 炉次草稿 `/api/drafts`

- 创建时按**顺序**给出 `recipeIds`：必须是 **2～3 个互不相同的现有配方
  ID**；引用一经创建**不可更改**（没有修改引用的接口）。
- 新建炉次只能引用**可用**状态的配方：引用任一**已停用**配方 → **422**
  （`detail` 指明配方 ID），**不保存炉次**；前端下拉同样只列出可用配方，
  页面尚未刷新时由服务端拦截。停用与新建并发时按某一先后顺序串行生效：
  停用先生效，则新建失败。
- 创建即**从 20°C 空记录求解**（复用原 BFS 规则），草稿**版本从 1 开始**；
  结果（有解时的完整曲线，或无解时的中文原因）与当时的材料参数快照一同
  保存。**无解草稿同样保存**，只是不保留任何曲线。
- **修改某配方时，所有引用它的“活动”草稿在同一事务里一起重算，版本各
  +1**：有解则写入新结果，无解则**清除旧曲线并记录原因**。已归档炉次不
  再受配方修改影响。
- **重算中的读取行为（不等待本次求解）**：`PUT` 耗时主要在重算求解；
  **无论走哪条重算分支**（常规的锁外求解，或连续竞争后的兜底路径），
  求解期间都**不持读锁、不写任何数据**；此时 `GET /api/recipes`、
  `GET /api/drafts`、`GET /api/drafts/{id}` 仍可立即读到**上一整次已提交
  的完整状态**（旧配方参数 + 各草稿旧参数快照与旧结果严格同版），
  **绝不会读到“新参数 + 旧结果”之类的未提交半成品**。待全部受影响草稿
  求解完成后，配方新参数、新版本与全部草稿的新参数快照、结果（或无解
  原因）、新版本才在**同一事务里一起生效**；`PUT` 仍等整体提交后才返回，
  响应结构（`recipe` + `recomputedDrafts`）、版本增量与「无解清除旧曲线」
  的行为均不变。求解或写库异常时本次修改整体回滚、全部不生效。
- 并发修改共享配方、并发创建引用炉次、停用/恢复与归档，仍与修改按**某一
  先后顺序串行生效**：求解期间若有他次写操作先生效，本次重算结果会整体
  作废并按最新状态重新求解（不会漏算新草稿、不会覆盖他次修改、不会改写
  已归档冻结方案）。
- **连续竞争时的读取行为**：若共享配方被连续修改、或不断出现引用该配方
  的新炉次，快照复核可能连续失效（默认 5 次）；此后转入兜底路径，但求解
  **仍然不持读锁**——上述三个 GET 在整个求解期间照常立即返回**上一整次
  已提交的完整状态**，不被阻塞，也读不到未提交参数。兜底路径只额外持有
  一把**写操作专用互斥锁**：其他写操作（修改 / 新建 / 停用 / 归档）在它
  上面排队，使本次修改的快照、求解与提交之间不再插入他次写操作，从而
  一次提交成功、严格落在他次修改之后；`PUT` 依旧等整体提交后才返回
  （单进程部署，无后台任务、无新服务）。
- 炉次读取（列表 / 详情）包含：`status`（`active` / `archived`）、
  引用配方与引用时版本 `recipeVersions`、草稿 `version`、求解所用
  `materials` 参数快照、`solvable`、`result`（完整求解结果）或 `reason`
  （无解原因）。活动草稿的参数与结果一定是同一次求解写入的，**不会读到
  “新参数 + 旧结果”的混合态**。

### 归档核准方案 `POST /api/drafts/{id}/archive`

请求体 `{"version": N}` 携带**你所查看的草稿版本**：

- 版本与当前不一致（期间配方被改过）→ **409**，响应体附 `currentVersion`，
  请重新读取最新参数与结果后再归档；
- 草稿当前**无解** → **422**（中文原因），**不改变任何状态**；
- 已归档 → 409；归档与配方修改并发时，由服务端按**某一先后顺序串行生效**，
  后到的归档会收到 409，先到的归档冻结旧版本；
- 成功 → 状态变为 `archived`，**冻结当时的材料参数、草稿版本与完整求解
  结果**；此后配方再怎么改，只影响其余未归档炉次。

### 前端：切换炉次时的请求编排（响应不串用）

「炉次草稿」详情区对查看 / 刷新 / 归档三类请求做了严格编排，避免慢响应
串用（接口字段、状态码与冻结规则均不变，全部为前端行为）：

- **只认当前选择的最新请求**：每次「查看」或「刷新」都生成新一代请求，
  详情、曲线、提示与归档按钮只接受最新一代的响应。先看甲再看乙时，晚
  返回的甲（无论成功、404/500 等失败还是网络异常）一律作废，**不会覆盖
  乙的详情或提示**。
- **等待期间仍可自由切换炉次**（包括“在甲等归档时切到乙、再切回甲”），
  也可再次点刷新（旧请求自动作废）。
- **查看或刷新一发起，旧详情立即失去归档资格**：在目标详情**成功读回
  之前**，归档按钮始终禁用（显示“详情加载中…”）；读取失败或网络异常时
  保持禁用并提示重试，杜绝把归档发到一份已经过期的详情上。
- **归档绑定点击那一刻的炉次 ID 与 `version`**：提交后即使立刻切到别的
  炉次，该归档请求仍照常发往服务端、照常生效；但它的成功 / 409 / 422 /
  网络异常**不会改写新炉次的详情、曲线、提示或按钮**（仅在成功时后台静默
  刷新列表状态）。新炉次读回后，完全按其自身状态（活动 / 已归档、版本）
  决定按钮。
- **切走再回到同一炉次**：回到甲（或对甲再次「刷新」）会生成新一代详情
  请求；此前那笔归档**晚到的回包对甲同样作废**——既不会清空 / 覆盖新读回
  的详情与曲线，也不会插入旧的成功或 409/422 提示，更不会代替用户再触发
  一次详情 GET。它只释放在途状态、按当前有效详情恢复归档按钮。若旧归档
  已成功，列表状态在后台静默同步；甲的最新状态以新 GET 读回的内容为准。
- **留在原炉次、期间没有重新查看或刷新**：归档成功显示冻结方案与成功
  提示，按钮转为“已归档”；409（版本过期 / 已归档）或 422（当前无解）
  都会**重新读取同一炉次**，并把中文原因**保留在页面上**（409 同时提示
  当前版本号）；网络失败不改详情，提示可重试。
- **同一炉次归档请求未结束前不得重复提交**（归档中按钮禁用并按炉次 ID
  去重，切走再回来也不解锁）；不同炉次的归档互不影响。归档按钮始终只按
  **当前有效详情**恢复：详情加载中、读取失败（含网络异常）或已归档时
  一律禁用。

## 求解算法

在档位最多 20、温度只有 19 个整十值的规模下，直接做广度优先搜索：

- 状态 = `(当前温度, 各材料连续保温进度)`；进度为“当前已连续处于其区间
  内的档数”，达标后记为完成态；
- 从零编排：从初态 `(20, 全 0)` 出发；带执行记录时：先逐档核验并重放
  记录得到 `(最后已执行温度, 当前进度)`，再以该状态为起点；
- 按温度**升序**扩展下一档（温差 ≤ 20、不超过耐受上限）。BFS 保证
  档数最少，同层按温度升序入队保证温度序列字典序最小；
- 目标状态：当前温度回到 20°C 且所有材料均已完成；
- 扩展深度（已执行档数 + 后续档数）超过 20 即停止；搜不到目标即为无解。
- 另含一个保守的无解预检（如保温下限高于所有耐受上限的最小值），
  仅在无执行记录时使用，用于给出更具体的原因；预检通过不代表必然有解，
  最终以 BFS 为准。

## API

`POST /api/schedule`

请求体字段：

- `materials`（必填，2～3 项）：每种材料的名称与温度参数；
- `executedTemperatures`（可选）：1～20 个已执行档位温度的整数数组；
  省略或传 `[]` 表示从零编排。

其余接口（配方与炉次管理，均返回 JSON）：

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/api/recipes` | 列出全部配方（含 `status`：`active` / `disabled`） |
| POST | `/api/recipes` | 创建配方（201，版本从 1 开始，默认可用） |
| GET | `/api/recipes/{id}` | 读取单个配方（含状态） |
| PUT | `/api/recipes/{id}` | 整份修改配方；原子重算引用它的活动草稿（版本 +1）；停用配方也可修改并保持停用 |
| POST | `/api/recipes/{id}/disable` | 停用配方（200；未知 404；不改参数/版本，不重算草稿） |
| POST | `/api/recipes/{id}/enable` | 恢复配方为可用（200；未知 404） |
| GET | `/api/drafts` | 列出全部炉次（含状态/参数/版本/结果或无解原因） |
| POST | `/api/drafts` | 按 2～3 个不同现有配方 ID 创建炉次并从空记录求解（201；引用已停用配方 422） |
| GET | `/api/drafts/{id}` | 读取炉次详情（归档后返回冻结快照） |
| POST | `/api/drafts/{id}/archive` | 携带查看版本归档（过期 409 / 无解 422） |
| POST | `/api/drafts/{id}/reuse-check` | 已归档炉次原曲线复用检查（只读当前配方，不重新求解；未知 404 / 未归档 422） |
| POST | `/api/drafts/{id}/electricity` | 已归档炉次电费试算（只读冻结曲线，不保存；未知 404 / 未归档或参数非法 422） |

### 从零编排（可直接复制执行）

```bash
curl -sS -X POST http://localhost:8000/api/schedule \
  -H 'Content-Type: application/json' \
  -d '{
    "materials": [
      {"name": "材料A", "holdLow": 60, "holdHigh": 100, "duration": 10, "tolerance": 150},
      {"name": "材料B", "holdLow": 80, "holdHigh": 120, "duration": 5,  "tolerance": 160}
    ]
  }'
```

返回（HTTP 200，节选）：

```json
{
  "solvable": true,
  "hasExecuted": false,
  "temperatures": [40, 60, 80, 60, 40, 20],
  "executedTemperatures": [],
  "continuationTemperatures": [40, 60, 80, 60, 40, 20],
  "executedCount": 0,
  "continuationCount": 6,
  "stepCount": 6,
  "totalMinutes": 30,
  "alreadyComplete": false,
  "toleranceCap": 150,
  "windows": [
    {"material": "材料A", "startStep": 2, "endStep": 3, "minutes": 10},
    {"material": "材料B", "startStep": 3, "endStep": 3, "minutes": 5}
  ]
}
```

### 带执行记录续炉（可直接复制执行）

已执行 `[40, 60, 80]` 三档：材料 A 已连续保温 2 档（区间 60～100），
材料 B 已保温 1 档（区间 80～120）。续排只需降温回 20°C：

```bash
curl -sS -X POST http://localhost:8000/api/schedule \
  -H 'Content-Type: application/json' \
  -d '{
    "materials": [
      {"name": "材料A", "holdLow": 60, "holdHigh": 100, "duration": 10, "tolerance": 150},
      {"name": "材料B", "holdLow": 80, "holdHigh": 120, "duration": 5,  "tolerance": 160}
    ],
    "executedTemperatures": [40, 60, 80]
  }'
```

返回（HTTP 200，节选）：

```json
{
  "solvable": true,
  "hasExecuted": true,
  "temperatures": [40, 60, 80, 60, 40, 20],
  "executedTemperatures": [40, 60, 80],
  "continuationTemperatures": [60, 40, 20],
  "executedCount": 3,
  "continuationCount": 3,
  "stepCount": 6,
  "totalMinutes": 30,
  "alreadyComplete": false,
  "steps": [
    {"index": 1, "temperature": 40, "deltaFromPrev": 20, "executed": true,  "phase": "executed"},
    {"index": 4, "temperature": 60, "deltaFromPrev": -20, "executed": false, "phase": "continuation"}
  ]
}
```

字段说明（续炉相关）：

| 字段 | 含义 |
| --- | --- |
| `hasExecuted` | 是否带已执行记录 |
| `executedTemperatures` / `executedCount` | 已执行档位温度与档数 |
| `continuationTemperatures` / `continuationCount` | 后续曲线温度与档数（可为空数组） |
| `alreadyComplete` | 已执行记录结束时是否已全部达标且回到 20°C（此时后续为空） |
| `steps[].executed` / `steps[].phase` | 每档属于 `executed`（已执行）还是 `continuation`（后续） |
| `windows` | 各材料首次达标区间，按**完整曲线**统一编号（允许横跨两段） |

### 已全部达标且回到 20°C —— 后续为空

```bash
curl -sS -X POST http://localhost:8000/api/schedule \
  -H 'Content-Type: application/json' \
  -d '{
    "materials": [
      {"name": "材料A", "holdLow": 60, "holdHigh": 100, "duration": 10, "tolerance": 150},
      {"name": "材料B", "holdLow": 80, "holdHigh": 120, "duration": 5,  "tolerance": 160}
    ],
    "executedTemperatures": [40, 60, 80, 100, 100, 120, 100, 80, 60, 40, 20]
  }'
```

返回（节选）：`"solvable": true, "alreadyComplete": true,
"continuationTemperatures": [], "continuationCount": 0`。

### 执行记录违规 —— HTTP 422，指出首个违规档

第 2 档由 40°C 跳到 80°C（温差 40°C > 20°C）：

```bash
curl -sS -X POST http://localhost:8000/api/schedule \
  -H 'Content-Type: application/json' \
  -d '{
    "materials": [
      {"name": "材料A", "holdLow": 60, "holdHigh": 100, "duration": 10, "tolerance": 150},
      {"name": "材料B", "holdLow": 80, "holdHigh": 120, "duration": 5,  "tolerance": 160}
    ],
    "executedTemperatures": [40, 80]
  }'
```

```json
{"detail": "已执行记录第 2 档温度 80°C 与上一档 40°C 温差 40°C，超过 20°C 上限。"}
```

超过耐受上限（所有材料耐受上限最小值为 150°C，第 7 档 160°C 违规）：

```bash
curl -sS -X POST http://localhost:8000/api/schedule \
  -H 'Content-Type: application/json' \
  -d '{
    "materials": [
      {"name": "材料A", "holdLow": 60, "holdHigh": 100, "duration": 10, "tolerance": 150},
      {"name": "材料B", "holdLow": 80, "holdHigh": 120, "duration": 5,  "tolerance": 160}
    ],
    "executedTemperatures": [40, 60, 80, 100, 120, 140, 160]
  }'
# {"detail":"已执行记录第 7 档温度 160°C 超过所有材料耐受上限的最小值 150°C。"}
```

### 无可行后续 —— HTTP 200，solvable=false

已执行档数 + 最短必要后续超过 20 档时（HTTP 200，便于页面区分“无解”与
“参数错误”）：

```json
{"solvable": false, "reason": "已执行 20 档后，在 20 档总上限内找不到满足剩余材料并回到 20°C 的后续曲线，无法续排。"}
```

参数非法（材料参数错误、已执行记录为空/超过 20 个/非整十数/越界等）
时返回 HTTP 422，`detail` 为中文错误说明。响应中**不会**附带任何部分
曲线。

### 配方与炉次草稿（可直接复制执行）

```bash
# 1) 创建两个材料配方（版本均从 1 开始，默认可用）
curl -sS -X POST http://localhost:8000/api/recipes \
  -H 'Content-Type: application/json' \
  -d '{"name":"钢甲","holdLow":60,"holdHigh":100,"duration":10,"tolerance":150}'
# {"id":1,"name":"钢甲","holdLow":60,...,"status":"active","version":1,"createdAt":"...","updatedAt":"..."}

curl -sS -X POST http://localhost:8000/api/recipes \
  -H 'Content-Type: application/json' \
  -d '{"name":"铜乙","holdLow":80,"holdHigh":120,"duration":5,"tolerance":160}'

# 2) 创建炉次草稿：按顺序引用 2 个不同现有配方，从 20°C 空记录求解
curl -sS -X POST http://localhost:8000/api/drafts \
  -H 'Content-Type: application/json' \
  -d '{"name":"十月首炉","recipeIds":[1,2]}'
```

返回（HTTP 201，节选）：

```json
{
  "id": 1,
  "name": "十月首炉",
  "status": "active",
  "recipeIds": [1, 2],
  "recipeVersions": {"1": 1, "2": 1},
  "version": 1,
  "materials": [
    {"name": "钢甲", "holdLow": 60, "holdHigh": 100, "duration": 10, "tolerance": 150},
    {"name": "铜乙", "holdLow": 80, "holdHigh": 120, "duration": 5,  "tolerance": 160}
  ],
  "solvable": true,
  "result": { "solvable": true, "temperatures": [40, 60, 80, 60, 40, 20],
              "stepCount": 6, "totalMinutes": 30, "steps": [ ... ],
              "windows": [ ... ], "sharedSteps": [ ... ], "materials": [ ... ] },
  "reason": null,
  "createdAt": "...", "updatedAt": "...", "archivedAt": null
}
```

### 修改配方 → 引用草稿一起重算（版本 +1）

```bash
curl -sS -X PUT http://localhost:8000/api/recipes/1 \
  -H 'Content-Type: application/json' \
  -d '{"name":"钢甲","holdLow":70,"holdHigh":100,"duration":10,"tolerance":150}'
```

```json
{
  "recipe": { "id": 1, "holdLow": 70, "version": 2, "...": "..." },
  "recomputedDrafts": [
    { "id": 1, "status": "active", "version": 2, "solvable": true,
      "materials": [ { "holdLow": 70, "...": "..." } ],
      "result": { "...": "与 materials 同版的完整结果" } }
  ]
}
```

非法修改（如保温下限高于上限、耐受上限低于保温上限、非整十数等）返回
**422 且不改变任何数据**：配方版本不增加，引用草稿也不会被重算。
新参数导致草稿无解时，草稿仍保存：`version` 增加、`solvable:false`、
`result:null`、`reason` 为中文原因。

### 停用配方 → 拒绝引用 → 恢复

```bash
# 停用配方 1：只切换状态，材料参数、配方/草稿版本不变，不重算任何炉次
curl -sS -X POST http://localhost:8000/api/recipes/1/disable
# {"id":1,"name":"钢甲",...,"status":"disabled","version":2,...}

# 停用期间新建炉次引用它 → 422，指明配方 ID，不保存炉次
curl -sS -X POST http://localhost:8000/api/drafts \
  -H 'Content-Type: application/json' \
  -d '{"name":"十月二炉","recipeIds":[1,2]}'
# 422：{"detail":"配方 ID 1 已停用，不能用于新建炉次。"}

# GET /api/recipes 仍列出全部配方并附状态
curl -sS http://localhost:8000/api/recipes
# [{"id":1,...,"status":"disabled",...},{"id":2,...,"status":"active",...}]

# 恢复为可用，之后可再次被新炉次引用
curl -sS -X POST http://localhost:8000/api/recipes/1/enable
# {"id":1,"name":"钢甲",...,"status":"active","version":2,...}

# 未知配方的状态操作 → 404
curl -sS -X POST http://localhost:8000/api/recipes/999/disable
# 404：{"detail":"配方 999 不存在。"}
```

停用不改变已有炉次：引用它的活动草稿在配方修改时照常重算、可正常归档，
已归档方案保持冻结；停用的配方本身也仍可整份修改（PUT）并保持停用。

### 归档：携带所查看的版本

```bash
# 正常归档（当前查看的就是 v2）
curl -sS -X POST http://localhost:8000/api/drafts/1/archive \
  -H 'Content-Type: application/json' -d '{"version":2}'
# 200，节选：
# {"id":1,"status":"archived","version":2,
#  "materials":[...冻结快照...],"result":{...冻结完整结果...},
#  "archivedAt":"2026-10-01T08:00:00Z", ...}

# 期间配方被改过（草稿已到 v3），仍带 v2 归档 → 409
curl -sS -X POST http://localhost:8000/api/drafts/1/archive \
  -H 'Content-Type: application/json' -d '{"version":2}'
# 409：{"detail":{"detail":"炉次草稿已过期：你查看的是版本 2，当前为版本 3，请重新读取后再归档。",
#                 "currentVersion":3}}

# 当前无解时归档 → 422，状态不变
# 422：{"detail":"材料「钢甲」的保温下限 170°C 已高于所有材料耐受上限的最小值 160°C，..."}
```

归档成功后再次 `PUT /api/recipes/1` 只影响其他**未归档**炉次；
`GET /api/drafts/1` 永远返回归档时冻结的版本、参数与结果。

### 已归档炉次电费试算 `POST /api/drafts/{id}/electricity`

对**已归档**炉次的冻结温度曲线做只读电费试算：不保存试算、不改动方案；
配方之后被修改或停用都不影响试算（始终读取归档时冻结的曲线与档数）。

请求体字段：

- `startMinute`（必填）：首档开始时刻距**当天 00:00** 的分钟数，
  `0～1439` 的整数（例如 1438 = 当天 23:58）；
- `prices`（必填）：按 `00～23` 时排列的 **24** 个电价（元/kWh），
  每天原样重复；每项为非负有限数；
- `powers`（必填）：与冻结曲线**等长**的逐档功率（kW），每项为非负
  有限数；每档 5 分钟内功率恒定，`kWh = 功率 × 5 / 60`。

分摊与舍入：

- 档位跨小时（含跨午夜）时，按实际落入各小时的分钟数分摊电量与费用，
  小时边界自然也是午夜边界（电价按天重复）；
- **逐档费用各自四舍五入到两位小数**；
- **总费用先汇总未舍入的逐档费用，再四舍五入到两位**（因此总费用可能
  与逐档舍入值之和相差 1 分，页面会明确标注）；总电量不另行舍入。

状态码：

- 炉次 ID 未知 → **404**（无论请求体是否合法）；
- 炉次存在但**未归档**（活动草稿，含当前无解）→ **422**；
- 已归档但参数非法（开炉分钟越界 / 非整数、电价不是 24 个、含负数 /
  NaN / Infinity、功率为空或长度与冻结曲线不一致等）→ **422**，
  `detail` 为中文说明。

可直接复制执行（假设炉次 1 已归档，冻结曲线为 6 档
`[40,60,80,60,40,20]`，23:58 开炉，每档 60 kW；00 时电价 0.8、
23 时 0.3、其余 0.5 元/kWh）：

```bash
curl -sS -X POST http://localhost:8000/api/drafts/1/electricity \
  -H 'Content-Type: application/json' \
  -d '{
    "startMinute": 1438,
    "prices": [0.8, 0.5, 0.5, 0.5, 0.5, 0.5, 0.5, 0.5,
               0.5, 0.5, 0.5, 0.5, 0.5, 0.5, 0.5, 0.5,
               0.5, 0.5, 0.5, 0.5, 0.5, 0.5, 0.5, 0.3],
    "powers": [60, 60, 60, 60, 60, 60]
  }'
```

首档 23:58～24:00 有 2 分钟在 23 时（0.3 元）、3 分钟在次日 00 时
（0.8 元）：`(2×0.3 + 3×0.8) / 60 × 60 = 3.00` 元；其后各档在 00 时
均为 `5 × 0.8 = 4.00` 元。返回：

```json
{
  "draftId": 1,
  "version": 1,
  "startMinute": 1438,
  "stepCount": 6,
  "totalMinutes": 30,
  "steps": [
    {"index": 1, "temperature": 40, "minutes": 5, "kwh": 5.0, "cost": 3.0},
    {"index": 2, "temperature": 60, "minutes": 5, "kwh": 5.0, "cost": 4.0},
    {"index": 3, "temperature": 80, "minutes": 5, "kwh": 5.0, "cost": 4.0},
    {"index": 4, "temperature": 60, "minutes": 5, "kwh": 5.0, "cost": 4.0},
    {"index": 5, "temperature": 40, "minutes": 5, "kwh": 5.0, "cost": 4.0},
    {"index": 6, "temperature": 20, "minutes": 5, "kwh": 5.0, "cost": 4.0}
  ],
  "totalKwh": 30.0,
  "totalCost": 23.0
}
```

跨普通小时边界的例子（`startMinute: 58`，即 00:58 开炉）：首档
00:58～01:03 拆成 00 时 2 分钟 + 01 时 3 分钟，费用
`(2×0.8 + 3×0.5) = 3.10` 元，之后各档均在 01 时（0.5 元）为 2.50 元，
总费用 15.60 元。

前端在「炉次草稿」详情中，仅**已归档**炉次显示「归档炉次电费试算」
卡片（开炉分钟、24 个分时电价、与冻结曲线等长的逐档功率）。切换炉次、
刷新详情或编辑任一试算输入时都会**立即清除旧试算**；试算请求自带代际，
此前请求的迟到成功 / HTTP 错误回包与网络异常一律作废，不会回填到当前界面。

前端对等待期交互的编排（仅前端行为，接口字段、状态码、冻结曲线与计费规则
均不变）：

- **等待中编辑立即解锁**：试算请求发出后，一旦编辑开炉分钟、电价或功率，
  立即清除旧结果与错误提示并恢复按钮，可用新输入**再次试算，无需等待旧
  请求结束**；旧请求随后无论成功、HTTP 错误还是网络异常，都不回填结果 /
  提示，也不改变当前按钮。
- **切换炉次 / 刷新详情同样作废**：一发起查看或刷新，旧试算即作废。只有
  **当前已归档详情成功加载后**才可提交试算；详情加载中、读取失败（含网络
  异常）或炉次未归档时，试算卡片隐藏或按钮不可用，杜绝向过期 / 不存在的
  归档发请求。
- **提交去重**：每次有效提交在等待**自身响应**期间禁用按钮（“试算中…”），
  并在提交入口去重，防止重复提交；被作废的旧请求不占用这一状态。
- **切走后又回到原炉次**：旧请求晚到的回包对新视图同样作废（成功也不
  回填），一切以重新读回的归档详情为准。
- **当前请求完成后恢复操作**：成功展示逐档试算结果；HTTP 错误（404/422
  等）在错误条显示后端中文原因；网络失败不改动数据并可直接重试——恢复与否
  只取决于当前这一代自身是否结束，**不受旧请求是否已经结束影响**。

### 已归档炉次原曲线复用检查 `POST /api/drafts/{id}/reuse-check`

对已归档炉次回答一个问题：**冻结的原曲线还能不能用于各配方的当前参数**。
只读核验、不重新求解，也不改动配方、草稿或归档。

- 按炉次创建时的**原配方引用顺序**，在**同一读取时点**读取各配方的当前
  参数与状态，逐项核验归档冻结曲线；
- 连续保温沿用原规则：每档 5 分钟，档位温度落在 `[保温下限, 保温上限]`
  内则连续进度 +1，达到所需档数即达标；离开区间时未达标进度归零，
  已达标者保持达标；
- 每项返回：配方 ID、当前 `version` 与 `status`、保温是否达标
  （`holdOk`）、**首次达标**的连续档位区间 `window`（`startStep` /
  `endStep`，从 1 计，无则 `null`）、**首次超过耐受上限**的档号
  `toleranceBreachStep`（从 1 计，无则 `null`）；耐受检查覆盖整条曲线
  （达标后仍逐档检查）；
- **停用配方同样完成温度检查**，仅其状态会使整体判为不可复用；
- 全部材料保温达标、无任何超限且配方均为可用时，`reusable` 才为
  `true`；
- 炉次 ID 未知 → **404**；炉次存在但未归档 → **422**。

可直接复制执行（全新实例上依次执行：建配方 → 建炉次 → 归档 → 检查）：

```bash
# 1) 两个配方 + 一个引用它们的炉次草稿（创建即求解，版本均为 1）
curl -sS -X POST http://localhost:8000/api/recipes \
  -H 'Content-Type: application/json' \
  -d '{"name":"钢甲","holdLow":60,"holdHigh":100,"duration":10,"tolerance":150}'
curl -sS -X POST http://localhost:8000/api/recipes \
  -H 'Content-Type: application/json' \
  -d '{"name":"铜乙","holdLow":80,"holdHigh":120,"duration":5,"tolerance":160}'
curl -sS -X POST http://localhost:8000/api/drafts \
  -H 'Content-Type: application/json' -d '{"name":"十月首炉","recipeIds":[1,2]}'

# 2) 归档炉次 1（冻结曲线为 6 档 [40,60,80,60,40,20]）
curl -sS -X POST http://localhost:8000/api/drafts/1/archive \
  -H 'Content-Type: application/json' -d '{"version":1}'

# 3) 复用检查：原曲线能否用于各配方的当前参数
curl -sS -X POST http://localhost:8000/api/drafts/1/reuse-check
```

第 3 步返回（HTTP 200）：

```json
{
  "draftId": 1,
  "version": 1,
  "stepCount": 6,
  "temperatures": [40, 60, 80, 60, 40, 20],
  "items": [
    {"recipeId": 1, "name": "钢甲", "version": 1, "status": "active",
     "holdOk": true, "window": {"startStep": 2, "endStep": 3},
     "toleranceBreachStep": null},
    {"recipeId": 2, "name": "铜乙", "version": 1, "status": "active",
     "holdOk": true, "window": {"startStep": 3, "endStep": 3},
     "toleranceBreachStep": null}
  ],
  "reusable": true
}
```

归档后若配方被修改（例如钢甲保温区间改为 90～100°C，曲线最高仅 80°C），
同一接口会给出逐项结论：`钢甲` 变为 `holdOk:false, window:null`，
整体 `reusable:false`；若某配方耐受上限被调低，则
`toleranceBreachStep` 给出首次超限档号（1 计）；配方被停用时该项温度
检查照常完成，但 `status:"disabled"` 使整体不可复用。

前端在「炉次草稿」的已归档详情中显示「原曲线复用检查」卡片：点击后逐项
展示配方 ID、当前版本与状态、保温是否达标、首次达标区间与首次超限档号，
并给出整体可复用结论。**切换炉次或刷新详情会立即清除结论**；检查请求自带
代际，旧响应（成功 / HTTP 错误 / 网络异常）一律作废，不会回填到当前界面，
等待期间按钮禁用、不重复提交。

## 目录结构

```
.
├── app/
│   ├── __init__.py
│   ├── main.py        # FastAPI 路由与参数校验（schedule / recipes / drafts / archive / 复用检查 / 电费试算）
│   ├── solver.py      # 规则常量、校验与 BFS 求解（配方与草稿复用）
│   ├── reuse.py       # 已归档炉次原曲线复用检查（只读核验，不重新求解）
│   ├── electricity.py # 已归档炉次电费试算（跨小时/午夜分摊、逐档与总费用舍入）
│   ├── db.py          # SQLite 连接、建表、全局读锁与写操作互斥锁
│   └── store.py       # 配方 / 炉次草稿 / 归档 / 归档曲线只读 / 复用检查 的事务化读写
├── static/
│   ├── index.html     # 页面（编排求解 / 材料配方 / 炉次草稿 三个标签页）
│   ├── styles.css     # 样式
│   └── app.js         # 表单、请求、结果与 SVG 曲线渲染
├── Dockerfile
├── docker-compose.yml # 含命名卷 planner-data → /data，持久化 planner.db
├── requirements.txt
└── README.md
```

数据库路径：环境变量 `PLANNER_DB`（Compose 中为 `/data/planner.db`）；
裸跑且 `/data` 不可写时自动落到仓库内 `data/planner.db`。
