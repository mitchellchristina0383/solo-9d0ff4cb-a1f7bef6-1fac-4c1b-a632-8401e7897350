# 单仓库 Git 合并队列（FastAPI）

模拟 GitHub Merge Queue 的核心语义：分支入队时登记提交号，按入队顺序逐个与当前
主干 `main` 合并，对**合成提交**跑样例测试；测试期间主干允许照常推进，只有
"测试通过 **且** 主干仍指向本次合并基底" 时才推进主干，否则作废重做。

面向隔离网场景额外提供**源分支更新 API**：开发者把新修订打成 Git bundle
上传，校验通过后可非快进替换非 main 分支；已入队项登记的旧提交由队列建立
保留引用，重启 + `git gc` 之后仍能处理。

## 1. 语义与实现

### 处理流程（`app/worker.py`，单 worker 线程，FIFO 每次只处理队首一项）

1. `POST /queue` 入队：读取分支当前 HEAD 提交号并冻结登记（后续分支再推送、
   甚至被 bundle 非快进替换都不影响本次验证的提交），状态 `PENDING`；
   同时为该提交建立 `refs/queue-retain/<sha>` **保留引用**（防 gc 回收）。
2. worker 取最早的 `PENDING` 项置 `TESTING`，记录当前主干 `base`。
3. 在中心 **bare 仓库**之外创建独立 linked worktree，检出 `base`，执行
   `git merge --no-ff <入队提交>` 生成合成提交。
4. 在合成提交的 worktree 中运行测试（`app/runner.py`：优先仓库根的
   `run_tests.sh`，其次 pytest / unittest discover）。**此阶段不持锁，主干可被
   外部照常推进。**
5. 测试通过后重读主干：
   - 主干仍等于 `base`：用 `git update-ref refs/heads/main <new> <base>`
     做**比较交换（CAS）**推进，成功 -> `MERGED`；
   - 主干已变化（或 CAS 失败）：本次 run 记为 `OBSOLETE`，合成提交丢弃，
     队列项回到 `PENDING`，下一轮以新主干重新合并、重新测试；
6. 基底未变时，合并冲突 -> `CONFLICT`、测试失败 -> `FAILED`，原因与测试输出
   入库，队列继续处理下一项；若处理失败的同时主干也变了，同样按作废重做处理
   （`tests/test_worker_logic.py` 覆盖该分支）。
7. 队列项进入终态（含取消）后，若已没有活跃项引用其登记提交，删除对应
   `refs/queue-retain/<sha>` 保留引用，**解除队列对该提交的保留**。

### 源分支更新（`app/bundle_update.py`，`POST /repo/update-branch`）

隔离网开发者投递新修订：`git bundle create out.bundle <branch>` 打成 bundle，
在 `/docs` 上传并填**目标分支**与**预期旧提交号**。服务端处理顺序：

1. `git bundle list-heads`：必须**恰好一个 `refs/heads/*` 引用**（只带一个分支）；
2. 在临时**空仓库**里 `git bundle verify` + `git fetch`：证明包自包含、
   可导入空仓库——薄包（缺前置提交）、坏包在此拒绝（400），现有分支不受影响；
3. 快速失败检查：目标分支不能是 `main`（400）、必须已存在（404）、
   当前头必须等于预期旧提交（409）、**新旧提交号必须不同**（400）；
4. 把包内分支抓取进中心仓库的暂存引用 `refs/mq-stage/*`（对象入库）；
5. `git update-ref refs/heads/<branch> <new> <old>` **CAS 替换**：
   验包期间旧头被并发改动时替换失败（409），现有分支保留；
   **两个请求并发替换同一旧头时最多一个成功**，成功者响应返回新提交号。

验包（第 1–2 步）不持任何应用锁、且在独立线程池执行，**验包期间入队等
请求照常处理**。替换允许非快进：旧提交随即脱离分支，若有队列项登记着它，
靠第 1 节的保留引用存活；替换完成后入队的项自然登记新提交。

### 状态机

```
PENDING ──取队首──> TESTING ──测试通过+CAS成功──> MERGED
   ^                  │
   │                  ├──冲突/失败且基底未变──> CONFLICT / FAILED（终态，放行下一项）
   │                  │
   └──OBSOLETE（主干先变）── 回到队首用新主干重做
```

### 模块

| 文件 | 职责 |
| --- | --- |
| `app/main.py` | FastAPI 路由，Swagger UI 在 `/docs`（可入队、可上传 bundle） |
| `app/worker.py` | 后台 worker、状态机、作废/重试逻辑 |
| `app/bundle_update.py` | bundle 校验（单分支、可导入空仓库）与 CAS 替换源分支 |
| `app/retention.py` | 入队提交的保留引用：钉住、终态释放、启动重建 |
| `app/git_tools.py` | bare 仓库、worktree、合并、`update-ref` CAS、保留引用 |
| `app/runner.py` | 合成提交上的测试执行（含超时） |
| `app/db.py` | SQLite 持久化（items + 每次尝试 runs）、线程锁 |
| `scripts/init_sample_repo.py` | 生成样例仓库（main + 4 个演示分支） |
| `tests/test_worker_logic.py` | worker 关键分支单元测试（mock git/runner） |
| `tests/test_branch_update.py` | bundle 更新/并发替换/提交保留的测试（真实 git） |

### 持久化与韧性

- SQLite（WAL）保存队列项与每次 run 的基底、合成提交、原因、测试输出；
- 进程重启时残留的 `TESTING` 项自动复位为 `PENDING` 重新验证；
- 入队提交的保留引用 `refs/queue-retain/<sha>` 使旧提交在非快进替换 +
  重启 + `git gc --prune=now` 后仍可用于合并验证；启动时按活跃项重建
  保留引用并清理失效引用；全部引用者终态后引用删除，对象可被正常回收；
- 登记提交对象若极端情况下已丢失，该项判 `FAILED` 终态而不是无限重试；
- worktree 用完即 `git worktree remove --force` 并 prune，异常路径也在
  `finally` 中清理。

## 2. 启动

### Docker Compose（推荐）

```bash
docker compose up --build
# 打开 http://localhost:8000/docs
```

容器入口 `scripts/entrypoint.sh` 会在数据卷为空时自动初始化样例仓库；
中心 bare 仓库、SQLite、worktrees 均在命名卷 `mq-data`（容器内 `/data`）。

### 本地直接运行

```bash
python -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
MQ_DATA_DIR=./data python scripts/init_sample_repo.py   # 首次
MQ_DATA_DIR=./data uvicorn app.main:app --reload
```

## 3. API（均在 /docs 可交互调用）

| 方法/路径 | 说明 |
| --- | --- |
| `POST /queue` `{"branch":"feature-good"}` | 分支入队，返回登记的提交号 |
| `GET /queue?status=PENDING` | 队列列表，可按状态过滤 |
| `GET /queue/{id}` | 单项详情 + 每次 run（含 OBSOLETE 记录与测试输出） |
| `POST /queue/{id}/cancel` | 取消排队中的项 |
| `POST /repo/update-branch` | 上传单分支 bundle + 目标分支 + 预期旧提交号，CAS 非快进替换源分支，成功返回新提交号 |
| `GET /repo/main` / `GET /repo/branches` | 主干提交号 / 分支列表 |
| `POST /repo/advance-main` | **演示用**：向 main 追加一个提交，模拟测试期间外部合入 |
| `GET /health` | 健康检查 |

### 源分支更新的命令行示例

```bash
# 隔离网开发者侧：基于最新分支（或重写历史）后打自包含 bundle
git bundle create feature-good.bundle feature-good

# 上传（预期旧提交号填分支当前头；/docs 表单同样可传）
curl -X POST localhost:8000/repo/update-branch \
  -F bundle=@feature-good.bundle \
  -F branch=feature-good \
  -F expected_old_sha=<分支当前40位提交号>
# 200 -> {"branch":"feature-good","old_sha":"...","new_sha":"..."}
# 400 -> 坏包/薄包/多分支包/目标是main/新旧相同；404 -> 分支不存在；409 -> 旧头已变
```

### 样例仓库的四个分支

- `feature-good`：干净合并、测试通过；
- `feature-conflict`：与 main 改同一行（README 标题），必然冲突；
- `feature-broken`：干净合并但自带一个失败测试；
- `feature-slow`：测试睡眠 8 秒，留出"测试期间推进主干"的操作窗口。

### 手工复现"测试中主干推进 → 作废重做"

```bash
curl -X POST localhost:8000/queue -H 'Content-Type: application/json' \
  -d '{"branch":"feature-slow"}'
# 看到状态进入 TESTING 后立刻：
curl -X POST localhost:8000/repo/advance-main -H 'Content-Type: application/json' \
  -d '{"filename":"mainlog.md","content_line":"landed during tests"}'
# 随后 GET /queue/{id}：第一个 run=OBSOLETE，项自动重试，第二个 run=MERGED
```

## 4. 实测结果（本仓库开发环境实际运行）

环境无 Docker 守护进程，使用本机 Python 3.11 + uvicorn 跑同一套代码
（Dockerfile/compose 仅做静态校验）。本轮（v1.1.0）实测记录：

1. **bundle 非快进替换**：基于 main 重写 `feature-good`（subtract 提交，
   与旧头无祖先关系）打成单分支 bundle 上传 -> 200，响应 `new_sha` 与实际
   提交一致，分支头更新，`merge-base --is-ancestor` 确认非快进。
2. **拒绝路径**：坏文件 400 且分支保持不动；薄包（`main..branch`，缺前置
   提交）400 提示无法导入空仓库；双分支 bundle 400；目标 main 400；
   预期旧头不符 409；分支不存在 404；新旧提交相同 400。
3. **并发替换同一旧头**：两个线程同时上传不同新提交、相同预期旧头 ->
   恰好一个 200（返回新提交号）一个 409，分支头等于胜出新提交
   （单元测试与 HTTP 端到端各验证一次）。
4. **验包期间照常入队**：60MB 随机文件的大包验证约 2.8s，期间 9 次
   `POST /queue` 全部 200，最大延迟 0.23s。
5. **重启 + gc 后处理旧队列项（核心场景）**：`feature-slow` 占住 worker 时
   入队 `feature-good`（登记旧提交 C1）-> 上传 bundle 非快进替换为 C2 ->
   再入队（登记 C2）-> 重启服务（TESTING 自动复位）-> 中心仓库
   `git gc --prune=now` -> C1/C2 对象均因保留引用存活 -> 队列排空后
   三项全部 `MERGED`：C1 项仍按旧提交合并（主干出现 subtract），C2 项
   按新提交合并（主干出现 divide）-> 全部终态后 `refs/queue-retain/`
   清空（保留已解除）。
6. **单元测试**：16 个用例全部通过，覆盖 worker 作废/重试分支、bundle
   验包与各拒绝路径、并发 CAS、保留引用的钉住/释放/启动重建、登记提交
   丢失判 FAILED。

历史（v1.0）实测：正常合并、冲突/失败不阻塞队列、测试中主干推进触发
OBSOLETE 重做的记录见 git 历史。

## 5. 未完成项 / 已知限制

- 本机没有 Docker 守护进程，**未实际执行 `docker compose build/up`**，
  仅校验了 compose YAML、Dockerfile 与入口脚本语法；首次在有 Docker 的
  环境运行时需要验证一次镜像构建。
- 单 worker 线程 + SQLite 进程内锁，只支持单实例部署；多副本需要把
  取队首改为数据库行锁/租约（当前 `take_next_pending` 的原子性仅在
  单进程内成立）。bundle 替换的 CAS 由 git `update-ref` 保证，跨进程
  也安全，但"入队钉住提交"的引用计数只在单进程内一致。
- 没有鉴权/认证，API 与演示用 `advance-main` 均无访问控制，定位为本地/内网演示。
- bundle 更新只支持**替换已存在的非 main 分支**：不创建新分支、不删除
  分支、不支持一次多个分支；bundle 内分支名与目标分支名允许不同
  （以表单目标分支为准，CAS 防误伤）。
- 保留引用按"提交号"计数，不区分分支；同一提交被多个活跃项引用时，
  全部结束才释放。释放失败只记日志，残留引用由下次启动重建时清理。
- 冲突只做检测与记录，不提供在线解决冲突、重建入队（re-queue）按钮；
  失败/冲突项需要人工修好分支后以新分支或重新入队方式再验证。
- 入队冻结的是分支 HEAD 提交号；分支被删除时该项直接判 FAILED，
  分支在入队后新增的提交不会被自动追踪。
- 测试命令约定为 `run_tests.sh` / pytest / unittest 三种，未做任意
  CI 配置解析；测试超时由 `MQ_TEST_TIMEOUT` 控制（默认 120s）。
- 无指标/监控面板，队列可观测性仅靠 SQLite 与 `/queue` 接口。
- 上传的 bundle 先落临时文件再校验，未设大小上限；超大包会占用
  `/tmp` 磁盘与验包时间（验包在线程池执行，不阻塞入队）。

