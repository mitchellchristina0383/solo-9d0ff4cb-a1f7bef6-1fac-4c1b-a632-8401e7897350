# 单仓库 Git 合并队列（FastAPI）

模拟 GitHub Merge Queue 的核心语义：分支入队时登记提交号，按入队顺序逐个与当前
主干 `main` 合并，对**合成提交**跑样例测试；测试期间主干允许照常推进，只有
"测试通过 **且** 主干仍指向本次合并基底" 时才推进主干，否则作废重做。

## 1. 语义与实现

### 处理流程（`app/worker.py`，单 worker 线程，FIFO 每次只处理队首一项）

1. `POST /queue` 入队：读取分支当前 HEAD 提交号并冻结登记（后续分支再推送不影响
   本次验证的提交），状态 `PENDING`。
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
| `app/main.py` | FastAPI 路由，Swagger UI 在 `/docs` |
| `app/worker.py` | 后台 worker、状态机、作废/重试逻辑 |
| `app/git_tools.py` | bare 仓库、worktree、合并、`update-ref` CAS |
| `app/runner.py` | 合成提交上的测试执行（含超时） |
| `app/db.py` | SQLite 持久化（items + 每次尝试 runs）、线程锁 |
| `scripts/init_sample_repo.py` | 生成样例仓库（main + 4 个演示分支） |
| `tests/test_worker_logic.py` | worker 关键分支单元测试（mock git/runner） |

### 持久化与韧性

- SQLite（WAL）保存队列项与每次 run 的基底、合成提交、原因、测试输出；
- 进程重启时残留的 `TESTING` 项自动复位为 `PENDING` 重新验证；
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
| `GET /repo/main` / `GET /repo/branches` | 主干提交号 / 分支列表 |
| `POST /repo/advance-main` | **演示用**：向 main 追加一个提交，模拟测试期间外部合入 |
| `GET /health` | 健康检查 |

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
（Dockerfile/compose 仅做静态校验）。实测记录：

1. **正常合并**：`feature-good` 入队 → 合成提交测试通过 → CAS 推进 main，
   状态 `MERGED`，main 从 `06c91ff` 推进到合并提交 `5f744e1`。
2. **冲突后队列继续**：`feature-conflict`（CONFLICT，原因含冲突文件
   `README.md` 与 git 输出）、`feature-broken`（FAILED，原因含
   `AssertionError: 1 != 2` 完整测试输出）均正确落终态，其后排队的
   `feature-good` 照常被处理并 MERGED，坏项不阻塞队列。
3. **测试中主干推进（核心场景）**：`feature-slow` 测试进行中调用
   `advance-main`，第一次 run 记 `OBSOLETE`（base `5f744e1` → 当前
   `d3911c6`），项回 PENDING 后以新基底重做，第二次 run `MERGED`
   （attempts=2）；`git log --graph` 确认外部提交 `d3911c6` 保留在最终
   主干历史中，未被覆盖。
4. **边界**：未知分支入队返回 404 并提示现有分支；`/docs` 返回 200；
   worktree 全部清理无残留；取消接口可用；重启后残留 TESTING 项自动复位重验。
5. **单元测试**：`tests/test_worker_logic.py` 3 个用例全部通过，覆盖
   "冲突时主干已变 → 作废"、"基底稳定冲突 → CONFLICT 放行"、
   "CAS 失败 → 作废 → 新基底重试成功"。

## 5. 未完成项 / 已知限制

- 本机没有 Docker 守护进程，**未实际执行 `docker compose build/up`**，
  仅校验了 compose YAML、Dockerfile 与入口脚本语法；首次在有 Docker 的
  环境运行时需要验证一次镜像构建。
- 单 worker 线程 + SQLite 进程内锁，只支持单实例部署；多副本需要把
  取队首改为数据库行锁/租约（当前 `take_next_pending` 的原子性仅在
  单进程内成立）。
- 没有鉴权/认证，API 与演示用 `advance-main` 均无访问控制，定位为本地/内网演示。
- 冲突只做检测与记录，不提供在线解决冲突、重建入队（re-queue）按钮；
  失败/冲突项需要人工修好分支后以新分支或重新入队方式再验证。
- 入队冻结的是分支 HEAD 提交号；分支被删除时该项直接判 FAILED，
  分支在入队后新增的提交不会被自动追踪。
- 测试命令约定为 `run_tests.sh` / pytest / unittest 三种，未做任意
  CI 配置解析；测试超时由 `MQ_TEST_TIMEOUT` 控制（默认 120s）。
- 无指标/监控面板，队列可观测性仅靠 SQLite 与 `/queue` 接口。
