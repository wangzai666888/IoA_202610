# 三角色逐秒 IoA 调度：Ubuntu 部署与验收

本文件保留 2026-10-25 第二阶段的三人操作步骤。三人真实模型整局已由用户在 Ubuntu 验收。2026-10-26 增加的九候选五人固定选队与连续通信接线见 [五人实施操作单](FIVE_TEAM_IOA_GUIDE.md)；通信窗口仍待后续实现。

## 1. 本次新增的边界

控制器每秒调用一次 IoA 会话，之后只读取三名成员的轮次状态和仿真服务的工具审计。它不读取目标真值，不替 Agent 设置航点，也不参与目标坐标转发。队伍仍是首局固定的 UAV_observer、UUV_leader、USV_marker；从第 1 秒起复用第 0 秒返回的 comm_id。

每轮 ID 为 `episode_id:仿真秒`。IoA 原协议中未使用的 `proposal_id` 在仿真客户端中承载这个 ID，原 Server 已会保留并转发该字段，因此无需重建 Server。三份客户端则必须使用本次的新 common/types/communication.py 和 communication_layer.py。

每个成员在收到新轮次 ID 时记录消息数和任务序号基线。max_turns=12 对比本轮新增消息；到达上限后程序要求结束讨论，而不是把整段历史再次当成 12 条。这个上限约束对话生成，不会让正在运行的工具任务消失。

讨论产生结论后，控制器还要等待三名成员均满足：本轮 ID 一致、已收到结论、没有 TO_START/IN_PROGRESS/FAILED 任务、没有等待回复的被分配成员。然后检查仿真服务在本秒实际收到标记请求；第 0 秒还要核对一次观测及三人的初始航点，每个 5 秒采样时刻要核对观测。缺失或错误会中止本局，不会推进仿真时钟。

## 2. 同步文件

将本次更新包解压并按原目录结构合并到 Ubuntu 工程根目录。文件为：

- common/types/communication.py
- im_client/communication/communication_layer.py
- im_client/communication/sim_round_state.py（新增）
- im_client/main.py
- simulator/service.py
- simulator/http_client.py
- simulator/ioa_run.py（新增）
- simulator/tests/test_ioa_run.py（新增）
- simulator/CONTINUOUS_IOA_RUN.md（新增）
- simulator/UBUNTU_IOA_WIRING.md
- simulator/HTTP_IOA_GUIDE.md
- simulator/README.md

保留 Ubuntu 已有的 dockerfiles/compose/.env、simulator/service_auth.local.json、三份 *.local.env、数据库和 outputs。更新包不包含身份文件或模型密钥。原工具镜像 react-agent:sim 和三个角色 YAML 没变，不需要重建工具镜像。

如果 Ubuntu 的 communication_layer.py 有另一端独立修改，请对比合并，不能用旧版覆盖本次新增的轮次字段。更新后可运行 `python3 -B -m unittest discover -s simulator/tests -v` 验证仿真侧；这不调用模型。

## 3. 更新 Docker 服务

以下命令在 Ubuntu 的 IoA 工程根目录运行。先确认没有其他控制器在执行 http_run、ioa_check 或 ioa_run。已有实验局应先完成或明确中止。

    docker build -f dockerfiles/client.Dockerfile -t ioa-client:sim .
    docker compose -p ioa-simulation -f dockerfiles/compose/simulator.yaml up -d --build
    docker compose -p ioa-capture -f dockerfiles/compose/capture.yaml up -d --force-recreate
    docker compose -p ioa-capture -f dockerfiles/compose/capture.yaml ps

三个客户端重新创建时会通过 Docker SDK 重新创建对应的三个工具容器，仍使用已构建的 react-agent:sim。Server 和 Milvus 保持原运行状态。

检查三个客户端的新状态接口（命令只打印轮次状态，不打印身份文件）：

    curl -fsS http://127.0.0.1:5060/openapi.json | grep round_status
    curl -fsS http://127.0.0.1:5061/openapi.json | grep round_status
    curl -fsS http://127.0.0.1:5062/openapi.json | grep round_status

若某个客户端没有新接口，检查是否真正重建 ioa-client:sim 并使用 `up -d --force-recreate`，而不是只执行 `restart`。仿真服务还需新 `/control/audit` 接口；调度器开始后会访问它。

## 4. 先跑三轮

建议先运行默认三轮联调：

    python3 -B -m simulator.ioa_run --max-rounds 3

每完成一轮，终端应输出“tasks and tool calls verified”。第三轮之后，控制器主动结束本局并导出回放；报告预期 `completed_rounds: 3`、`time: 3`、`status: aborted`、`error: null`。这里的 aborted 是预定短程检查，不代表模型失败，也不会预期捕获。

控制器写入 `simulator/outputs/<episode_id>.ioa-run.json`，保存每轮的 comm_id、三方任务状态、真实工具操作、IoA 结论和最终回放文件名。每局结束后还会按该 comm_id 从 IoA Server 导出 `<episode_id>.ioa-chat.json`（Server 的完整组内消息记录）和 `<episode_id>.ioa-chat.txt`（按仿真轮次整理、便于阅读的版本），文件名列在报告的 `artifacts` 中。检查所有轮次的 comm_id 相同；第 0 秒应有观测、三人航点和标记，第 1、2 秒至少有标记。HTML 会显示真实推进到第 3 秒的运动。

若三轮成功，再根据模型调用成本运行整局：

    python3 -B -m simulator.ioa_run --max-rounds 0

0 表示运行到捕获、目标逃脱或仿真时限。模型调用所耗的现实时间不进入仿真秒数。模型采用的航点可能与无模型脚本不同，因此捕获时间不要求等于 18 秒；脚本基准的 18 秒仍用于验证仿真核心。

对话导出默认访问 Ubuntu 主机上的 `http://127.0.0.1:7788/fetch_chat_record`。若 IoA Server 使用其他地址，可给 `ioa_run` 传 `--ioa-server-url`。导出失败时，原有仿真结果、动画和 `.ioa-run.json` 仍会保存，报告的 `chat_export_error` 会说明原因，命令以非零状态退出，避免正式实验漏存对话。Server 的聊天数据库在当前 Compose 配置中没有挂载到主机，应在重建 Server 容器前检查导出文件。

这次增量同步只需覆盖 `simulator/ioa_run.py` 和 `simulator/CONTINUOUS_IOA_RUN.md`、`simulator/README.md`，新增 `simulator/ioa_chat.py` 与 `simulator/tests/test_ioa_chat.py`。只改主机上的仿真调度代码，不需重建 IoA 客户端、Server 或工具镜像。Server 对话记录是发送给队友的消息；模型的私有提示与工具 Agent 的逐步日志不包含在该记录中。

## 5. 失败时怎么看

- `required tool calls missing`：本秒未看到必要操作；看报告中上一个完整轮次和对应 IoA 会话。模型文字声称完成不算工具执行。
- `failed tool tasks`：至少一个工具任务抛错或 ReAct 明确返回失败；查该角色工具容器日志。
- `round barrier timed out`：讨论已结束但仍有未完成任务/未收齐回复，或某个成员没有进入本轮；检查各客户端的 `/simulation/round_status?comm_id=<报告中的ID>`。
- 若终端只有 `TimeoutError: timed out`，先参考 [TIMEOUT_RECOVERY.md](TIMEOUT_RECOVERY.md) 读取三名成员状态和该秒审计，再更新带阶段诊断及有限提示词窗口的代码。
- `round_error`：在上一轮尚未结束时收到新一轮；确认只有一个控制器运行，三份客户端均已更新。
- 409：模型试图在控制器已推进后提交旧 `expected_time`。旧任务不自动改成新时刻重试；本局应中止并排查生命周期。

通常异常会由控制器主动 abort 并生成报告。若进程被强行终止、留下 running 局，先重启三个客户端以停止旧任务，再执行：

    docker compose -p ioa-capture -f dockerfiles/compose/capture.yaml restart
    python3 -B -m simulator.ioa_run --abort-active

然后开始新一局。不要让旧工具任务继续运行时 reset。可用此前的本地 HTTP 回放服务器查看该局 HTML。

## 6. 如何理解本阶段结果

这一步验证的是“三人固定队伍 + 连续通信 + 逐秒执行”的技术闭环。它尚不是 10 场景 × 3 条件的正式通信窗口实验。稳定跑完整局并冻结模型提示、max_turns 与调用规则后，下一阶段才引入九候选五人固定选队，再加入规定时段消息排队与开放后交付。
