# MCP Test Suite Framework

把「MCP 能力验收」从**每次临场想测什么**变成**可维护、可执行、可对比**的资产。

一句话：**一次排障只值一次，写成用例值每一次。**

---

## 为什么做这个

我们内部的四个 MCP 服务每次上线前都要验收。此前的做法是：

1. 找一份当时的验收记录（散在 `reports/*.md`、飞书聊天、某个人的 shell 历史里）；
2. 照着敲一遍 curl；
3. 凭记忆补几条「上次踩过的坑」。

这条路径有三个必然的失效：

| 失效 | 具体形态 | 代价 |
|---|---|---|
| **不可复用** | 结论写在报告里，不是写在可执行文件里；下次从零再来 | 每次验收 1–2 小时 |
| **不可对比** | 上一轮「54 通过」和这一轮「56 通过」是不是同一批断言？没人知道 | 回归被漏掉也看不出来 |
| **不可评审** | 测了什么只存在于执行者的脑子里 | 覆盖盲区无法被指出来 |

更糟的是**假绿**：一条忘了写断言的用例、一个拼错的断言键，都会静默通过。
所以本框架把「结构正确」也当成硬约束 —— 缺三件套、未知断言键、重复 id 一律在加载期报错。

**验收结论必须能回答三个问题**：哪一层测了？哪一层没测？哪条期望与实际不符？

---

## 目录结构

```
mcp-test-suite/
├── mcp_suite/            # 框架本体（stdlib + pyyaml + httpx，无重框架）
│   ├── loader.py         # 读 YAML + 校验 schema（所有结构错误 → 退出码 2）
│   ├── runner.py         # 执行 tool（MCP Streamable HTTP）/ http 两类动作
│   ├── assertions.py     # 12 个 expect 键 → pass/fail + 期望 vs 实际
│   ├── report.py         # JSON / Markdown / 控制台三种视图
│   ├── emit_doc.py       # YAML → MCP-TEST-SUITE.md（人读版）
│   └── cli.py            # python -m mcp_suite run|validate|doc
├── templates/case.yaml   # 新增用例的模板（复制即用）
├── tests/                # 框架自身测试（假 MCP 服务，全离线）
├── Makefile              # make test / run-all / baseline-all / doc-all
├── run.sh                # ./run.sh db|elk|jenkins|sw|all|baseline
└── requirements.txt      # pyyaml + httpx
```

套件 YAML **不在本仓库**，而是放在每个被测 MCP 仓库的根目录：
`<mcp-repo>/mcp-test-suite.yaml` + 渲染出的 `<mcp-repo>/MCP-TEST-SUITE.md`。

为什么放被测仓库：**改动工具面的人，和改动用例的人，应该是同一个人**。
放在一个集中的仓库里，工具一改，用例就悄悄过期了。

---

## 快速开始

```bash
# 1) 依赖（两个）
python3 -m pip install -r requirements.txt

# 2) 框架自身的离线测试（不碰网络）
make test

# 3) 跑某个服务（令牌从该仓库的 .env 读，不写进任何文件）
./run.sh db
make run-db
make run-elk
make run-jenkins
make run-sw

# 4) 四个服务全跑
./run.sh all
make run-all

# 5) 只跑基线四层（liveness/auth/guard/admin）
./run.sh baseline
make baseline-all

# 6) 直接用 CLI（等价于 Makefile 里的命令，便于 CI）
export MCP_TEST_TOKEN=<业务令牌>
export MCP_TEST_ADMIN_TOKEN=<管理令牌>
python -m mcp_suite run --suite ../<service>/mcp-test-suite.yaml \
    --md /tmp/db-suite.md --json /tmp/db-suite.json
```

**退出码是契约**：`0` 全过 / `1` 有失败 / `2` 配置错误（YAML 结构、缺令牌、过滤后无用例）。
「配置错误」与「有失败」必须分开：前者说明**报告本身不可信**，后者才是被测服务的问题。
混成一个码，夜间任务里就分不清「服务坏了」还是「套件坏了」。

### 常用参数（都是可选，不影响 YAML）

| 参数 | 作用 |
|---|---|
| `--base-url` | 覆盖 `defaults.base_url`（例如指向本地实例） |
| `--layer auth,guard` | 只跑这些层（可重复） |
| `--priority P0` | 只跑这些优先级 |
| `--json/--md` | 报告落盘 |
| `--env-file <path>` | 从 dotenv 读令牌，**不覆盖**已存在的环境变量 |
| `--token-env / --admin-token-env` | 覆盖 YAML 里的环境变量名（本次运行有效） |
| `--consumer <name>` | 环境变量是 `token:消费者名` 列表时，按名字挑令牌 |
| `--retry-429 N` | 被限流时按 `Retry-After` 重试的次数（默认 5） |
| `--pace-ms N` | 两次请求之间的最小间隔（默认 0，靠 429 重试兜底） |

`--env-file` 的语义刻意是「只补空缺」：`export MCP_TOKEN=...` 是本次运行的显式意图，
被 `.env` 里的旧值盖掉会非常难查。

---

## 格式（固定 schema）

每个 MCP 仓库根目录一份 `mcp-test-suite.yaml`。用例三件套必须是
**目标（为什么测）/ 操作（怎么触发）/ 预期指标（怎么判定通过）**。

```yaml
suite: service-a
version: 1
service: service-a
updated: "2026-10-23"
owner: platform

defaults:
  base_url: "http://<your-mcp-host>:18091"
  timeout_s: 30
  # 令牌只从环境变量取，**绝不写进本文件**
  token_env: MCP_TEST_TOKEN
  admin_token_env: MCP_TEST_ADMIN_TOKEN

layers: [liveness, auth, guard, domain, governance, admin]

cases:
  - id: db-guard-003
    title: 资源消耗函数（SLEEP）必须被拒
    layer: guard
    priority: P0          # P0 上线必过 / P1 重要 / P2 一般
    goal: |
      单条 SLEEP 可占住数据库连接 30 秒，且 MySQL 在 MAX_EXECUTION_TIME 打断 SLEEP 时
      会返回 1 而不是报错 —— 曾导致「被中断的查询以成功返回」（真实事故形态）。
    given:                 # 人读的前置条件（渲染到文档里）
      datasource: <datasource-name>
      customer_id: "<tenant-id>"
    action:
      kind: tool           # tool | http
      tool: query
      arguments:
        datasource: <datasource-name>
        customer_id: "<tenant-id>"
        sql: SELECT SLEEP(1)
    expect:                # 机械判定的预期指标
      is_error: true
      error_type: SQL_NOT_ALLOWED
      max_elapsed_ms: 2000
    metric: "拒绝率 100%；P95 响应 < 2s"   # 验收结论（人读）
```

`action.kind: http` 用于 `/health`、`/ready`、`/admin/v1/*`：

```yaml
    action:
      kind: http
      method: GET
      path: /ready
    expect:
      status: 200
      json_path: status
      json_equals: ready
```

http 动作的三个可选字段（都在 `action` 里，不是 `expect`）：

* `body` —— POST JSON-RPC（如 `tools/list`）时用：mapping 会被 JSON 序列化；
* `headers` —— 例如故意带一个无效令牌：`Authorization: "Bearer sk-...invalid"`；
* `auth` —— `none | business | admin`。**默认按路径推断**：`/admin/**` → 管理令牌，
  `/mcp` → 业务令牌，其它 → 不带。要测「没带令牌必须 401」就显式写 `auth: none`。

### `expect` 支持的断言键（就这 12 个，不扩散）

| 键 | 含义 |
|---|---|
| `status` | HTTP 状态码 |
| `is_error` | MCP `result.isError` |
| `error_type` | `structuredContent.error_type`（程序化判定失败原因） |
| `json_path` | 定位 JSON 里的某个值；支持 `a.b[0].c` 与带连字符的键 |
| `json_equals` | 该路径的值与期望**精确相等** |
| `json_contains` | 该路径的值包含期望（list 找元素 / dict 子集 / str 子串） |
| `json_len` | 该路径的值的长度（list / str / dict） |
| `contains` | 响应文本包含该子串 |
| `not_contains` | 响应文本**不**包含该子串（防泄漏 / 防回退） |
| `max_elapsed_ms` | 本次请求耗时不超过该值（SLO） |
| `rows_equals` | `structuredContent.rows` 精确相等 |
| `min_rows` | `structuredContent.rows` 至少这么多行 |

两个刻意的约束：

1. **一条用例只能有一个 `json_path`**。要断言两个字段就写两条用例 ——
   一条用例只证明一件事，失败时不用再拆分归因。
2. **`json_path` 的根**：tool 动作是 `structuredContent`（没有时退回文本解析出的 JSON）；
   http 动作是响应体（SSE 帧会被拆开）。路径中途遇到「像 JSON 的字符串」会自动展开一层，
   所以把结果序列化进 `{"result": "..."}` 的服务也能直接断言内部字段。

### 用例可选字段

| 字段 | 作用 |
|---|---|
| `given` | 人读的前置条件，渲染到文档的「操作」列 |
| `metric` | 验收结论（人读），渲染到「预期指标」列末尾 |
| `regression` | 真实事故/工单编号（字符串或列表），报告里有「回归项映射」表 |
| `status` | `active`（默认）/ `todo`（不执行，报告里单列）/ `skip`（人工跳过） |

`status: todo` 是给「确认不了」的用例留的位置：它**不执行、不计入通过率**，
但会出现在报告的「待补」段里。这样「没测」不会被误读成「没问题」。

### 层（layers）与优先级

六个层是固定的，不允许自定义 —— 层的价值在于**每个服务用同一把尺子**：

| 层 | 语义 | 典型问题 |
|---|---|---|
| `liveness` | 进程活着 / 配置可读 / 就绪 | /health 与 /ready 的语义混用 |
| `auth` | 认证与越权 | 业务令牌能不能改配置 |
| `guard` | SQL / 参数护栏 | 「能查但不该查」 |
| `domain` | 业务能力 | 能不能真的干活 |
| `governance` | 限流 / 配额 / 审计 / 脱敏 | 日志里的凭据会不会流出 |
| `admin` | 管理面 | 版本号、配置、观测端点 |

优先级只有 `P0` / `P1` / `P2`。**P0 全过才算通过**（报告里单独给 P0 门槛结论）。

---

## 覆盖矩阵怎么读

报告里有两张汇总表，它们是**唯一**的验收结论口径：

```
## 覆盖矩阵（layers × 结果）

| 层 | 语义 | pass | fail | todo | skip | 用例数 |
| `liveness` | 进程活着 / 配置可读 / 就绪 | 7 | 0 | 0 | 0 | 7 |
| `guard`    | SQL / 参数护栏 | 13 | 0 | 0 | 0 | 13 |
| `domain`   | 业务能力 | 23 | 0 | 1 | 0 | 24 |
```

读法：

1. **先看 `用例数 = 0` 的层**。0 条不是「没问题」，是**没测**。一个服务 domain 层 0 条，
   说明这轮验收根本没验证它能不能干活。
2. **再看 `todo` 的数量**。todo 是「我知道这里该测，但还没确认」。todo 多说明覆盖在打折，
   而不是「待补项都通过了」。
3. **最后看 `fail`**。fail 的定义是「至少一个断言不成立」或「执行出错（超时/连接失败）」。
   报告里每条失败都带**期望 vs 实际**，不需要重新跑一遍去猜。

按优先级汇总表回答另一个问题：**这轮能不能上线**。
`P0` 有 fail 就是不能 —— 与 P1/P2 的表现无关。

---

## 怎么加一条用例（三步）

**第一步：复制模板**

```bash
cp templates/case.yaml /tmp/new-case.yaml   # 或者直接从模板里复制那一段
```

**第二步：填三件套，粘到目标仓库的 `mcp-test-suite.yaml`**

```yaml
- id: db-guard-014          # 唯一；小写字母/数字/连字符/下划线
  title: 一句话说明验证什么
  layer: guard
  priority: P0
  goal: |
    为什么值得测：出过什么问题、谁会踩、不测会怎样。不要复述操作。
  action:
    kind: tool
    tool: query
    arguments: { ... }
  expect:
    is_error: true
    error_type: SQL_NOT_ALLOWED
  metric: "拒绝率 100%"
```

**第三步：校验 + 渲染 + 跑它**

```bash
python -m mcp_suite validate --suite ../<service>/mcp-test-suite.yaml
python -m mcp_suite doc      --suite ../<service>/mcp-test-suite.yaml   # 重新生成 MD
python -m mcp_suite run      --suite ../<service>/mcp-test-suite.yaml --layer guard
```

三个都会在**加载期**报结构错误（缺三件套 / 未知 expect 键 / 重复 id / 未知 layer），
不用等到跑起来才发现。

---

## 与单元测试的关系

**单元测试测实现，本套件测契约 / 行为。两者不替代。**

| | 单元测试 | 本套件 |
|---|---|---|
| 对象 | 函数 / 类 | 已经跑起来的 HTTP 服务 |
| 环境 | mock 掉数据库、ES、Jenkins | 真实下游 + 真实令牌 |
| 能发现 | 逻辑分支写错、回归 | 部署配错、鉴权配错、协议形状漂移、跨服务契约不一致 |
| 发现不了 | 部署配错（mock 里没有环境） | 内部实现细节（只看得见行为） |

四个服务各自的单元测试都跑得不错，但线上仍然出过这些事 —— 它们**只有**套件能抓：

* 修复 `MAX_EXECUTION_TIME` 后，`SLEEP(35)` 仍以 `isError=false` 返回（实现层单测覆盖不到 MySQL 语义）；
* 令牌白名单改完，`list_datasources` 还在列 `b2b-*`（配置 + 部署问题）；
* 某个服务的授权组配置没有 `grants`，所有工具调用 `policy_denied`，而单测全绿
  （单测不起服务、不读 groups.yaml）；
* 服务 C 的参数校验失败没有 `structuredContent.error_type`（协议契约问题）。

**分工建议**：改实现 → 跑单测；改工具面 / 配置 / 鉴权 / 部署 → 跑本套件；
上线前两者都要。

---

## 四个服务的现状（2026-10-23）

| 服务 | 套件 | 用例数 | 说明 |
|---|---|---|---|
| 服务 A | 全量 | 59（57 active + 2 todo） | 含 2026-10-23 线上验收的全部断言 + MCP-01..13 回归映射 |
| 服务 B | 基线 + 可确认的 domain | 27（22 active + 5 todo） | domain 多为 todo：授权组当前阻断业务面（见报告） |
| 服务 C | 基线 + 可确认的 domain | 27（23 active + 4 todo） | 只读令牌基线；诊断/日志截断的精确口径待补 |
| 服务 D | 基线 + 可确认的 domain | 26（23 active + 3 todo） | 预检工具对未知表的判定待确认 |

「todo」不是偷懒，是**明确标记「这里还没确认」**。写一条猜出来的断言比不写更糟：
它会让整份报告看起来全绿。

---

## 写用例的两条硬规矩（都是踩出来的）

### ① 护栏类用例必须**成对**：测"该拒的拒了"，也要测"该放的没被拒"

只测拒绝方向，等于只测了一半。真实案例（2026-10-23）：

某服务的「执行前预检」长期没生效（计划数据形状不对，判据被静默跳过）。
修好之后立刻出现反向问题：**大表 `LIMIT 1` 被误杀** —— 用户只想采样一行，
却收到 `SQL_TOO_EXPENSIVE`。而我们当时的用例只覆盖了"该拒的拒了"，
所以这个 P1 是客户发现的。

所以护栏用例请按这个模式成对写：

```yaml
- id: guard-016          # 正向：该放的要放
  sql: SELECT UUID FROM big_table LIMIT 1
  expect: {is_error: false, max_elapsed_ms: 5000}

- id: guard-017          # 反向：该拒的要拒
  sql: SELECT COUNT(*) FROM big_table
  expect: {is_error: true, error_type: SQL_TOO_EXPENSIVE}
```

两条一起才证明规则判的是**风险**（扫描量），而不是**语法特征**（有没有 `LIMIT`）。

### ② 修好一个被长期掩盖的机制，第一件事是补齐它的正向用例

被掩盖的机制有个共同点：**它的失败是静默的**（不报错、只是不起作用）。
你修好它的那一刻，它第一次真正开始拦截 —— 也会第一次开始误杀。
此时既有用例通常只覆盖"修复前的样子"，必须补：

1. **正向**：正常输入不被误杀（最重要，也最容易被忘）
2. **边界**：阈值两侧各一条（刚好过 / 刚好不过）
3. **仍然生效**：原本该拦的还拦得住（防止修 A 弄坏 B）

### ③ 版本号这类"锚点"要用测试钉住，不能靠人记得

客户按 `initialize` 报的版本判断"线上有没有这个修复"。若代码里的版本常量
与清单文件（`service.yaml` / `pyproject.toml`）漂移，客户会得出**相反**的结论。
所以：**两边各留一处 + 一条断言两者相等的测试**，比写十条注释有用。

## 已知限制（刻意不做，不是遗漏）

1. **不能断言响应头**（如 `X-RateLimit-*`）。`expect` 的 12 个键都在 body 上。
   需要时先确认实现方案（body 字段 vs 响应头），再决定是否扩键 —— 本套件刻意不扩散。
2. **一条用例只能有一个 `json_path`**（见上）。
3. **只支持单动作**。像「先取最近一次构建号，再诊断那次构建」的两步用例写不了。
   这是 schema 的取舍：单动作才可机械判定、可并行、失败可归因。
4. **没有文件断言**（审计台账、schema 同步日志都在文件里）。当前只走 HTTP。
5. **429 是「验收工具自己的突发流量」**：一个 Runner 只做一次 initialize，
   并对 429 按 `Retry-After` 重试；但**断言 429 的用例不重试**（否则限流永远测不出来）。

---

## 反向验证（确认 runner 本身没骗你）

验收工具最大的风险是「它永远绿」。用一个**已知错误**的期望值去跑，必须变红：

```bash
# 复制一份，把某条用例的 error_type 改成错误值
cp ../<service>/mcp-test-suite.yaml /tmp/negative.yaml
sed -i '' '0,/error_type: SQL_NOT_ALLOWED/s//error_type: SQL_ALLOWED/' /tmp/negative.yaml

python -m mcp_suite run --suite /tmp/negative.yaml --layer guard
# 预期：该条 FAIL，并打印「期望 SQL_ALLOWED / 实际 SQL_NOT_ALLOWED」；退出码 1
```

这个自检应该出现在**每次改 runner 之后**。`make test` 里的
`test_wrong_expectation_fails_with_expected_and_actual` 是同一件事的离线版。

---

## 设计取舍

**为什么 YAML 单一来源 + 渲染 MD，而不是手写两份？**

同一份验收资产有两种读者：机器（runner）和人（评审、交接、客户对接）。
手写两份的漂移**一定**会发生，而且通常发生在「临时改一条断言看结果」之后 ——
改完忘了同步另一份。漂移的验收文档比没有文档更糟：它让人相信某条已被覆盖。

所以：YAML 是唯一事实来源，`MCP-TEST-SUITE.md` 由 `python -m mcp_suite doc` 单向渲染，
产物顶部明确写「不要手改」。渲染是确定性的（同一份 YAML 两次渲染结果完全一致），
所以 MD 进 git 后，diff 只会反映 YAML 的真实改动。

**为什么不用 pytest / 不引依赖？**

多一个依赖就多一个「装不上」的理由。这套东西要在任何一台有 python3 的机器上
（包括运维跳板机）直接跑起来。12 个断言键 + 两类动作的能力，不需要框架来提供。
