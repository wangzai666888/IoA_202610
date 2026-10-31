# 九候选、五人固定选队：连续通信接入与验收

更新日期：2026-10-26。三人真实模型整局已由用户在 Ubuntu 跑通。本次增加五人场景、固定选队、配置生成与五人调度检查。Windows 已验证无模型仿真和真实 HTTP 接口；五人真实模型结果需要在 Ubuntu 验收。

2026-10-26 补丁：五人完整局在 t=5 曾因队长模型回复缺少 `content` 字段，导致后台会话任务退出，最终表现为 `launch_goal` 等待 1800 秒超时。已加入模型回复格式校验与一次修正重试；连续格式错误或其他后台异常会写入 `round_error`，使等待中的请求及时报告原因。已部署旧版的用户只需同步补丁中的客户端源码、重建 `ioa-client:sim` 并重建五个客户端容器。具体步骤见 [超时排查操作单](TIMEOUT_RECOVERY.md) 的“五人 t=5”小节。该补丁不要求重建仿真服务、工具镜像、Server 或 Milvus。

2026-10-26 第二次补丁：五人局在 t=11 曾出现两名标记者都在 IoA 对话中声称调用 `sim_try_tag`，但工具实际返回 `HTTP 409: stale episode_id`，审计中没有 `tag`。日志里的真实局 ID 是 32 位，而模型提交的 ID 少了一个字符。现在客户端把本轮 `sim_round_id` 作为结构化字段传给 ReAct 工具容器；写工具从该字段取得局 ID 和仿真秒，模型只填写航点坐标，标记工具不需参数。延迟到下一秒的旧任务仍携带旧轮次，服务端仍返回 409。工具调用审计与捕获判定保持严格。本补丁需同步 `im_client/communication/communication_layer.py`、`im_client/agents/react/react_agent_adapter.py`、`im_client/agents/react/react_agent.py`、`simulator/ioa_tools.py`、`simulator/ioa_run.py`、`simulator/ioa_check.py`，并重建 `ioa-client:sim` 与 `react-agent:sim`。无需重建仿真服务、IoA Server、Milvus 或前端。

2026-10-26 第三次补丁：五人完整局在 t=0 曾被初始航点与标记的顺序检查中止。原审计只看本秒第一条标记；该局第一条标记早于两名成员设置航点，但所有五人随后均设置航点，而且两名标记者都在全部航点完成后再次真实调用了标记。由于仿真时钟在本轮结束前不推进，过早的失败标记不改变运动或捕获判定。现在审计要求两名观测者先于标记完成观测、五名成员都提交初始航点，并要求每名标记者至少有一条位于全部初始航点之后的真实标记；过早标记仍保留在原始审计中。只有过早标记而没有后续有效顺序的标记者仍使该局失败。本补丁只需同步 `simulator/ioa_run.py`、`simulator/tests/test_five_team.py` 和本指南；不需要重建 Docker 镜像或重启容器。

## 1. 范围与规则

2026-10-26 第四次补丁（next_people 校验）：失败局 0683ebd216ee4c7db77e9b55a2431141 在 t=4 的两次标记均已进入审计，但队长后续回复两次未通过 next_people 类型检查，round_error 因此使 /launch_goal 返回 HTTP 500。原提示词说结束讨论时 next_people 不适用，而校验器对所有消息都要求字符串或列表。现在提示词明确结束消息填 []；解析器也兼容结束消息的 null 或省略字段，并使用原有“当前发言者收尾”的路由。其他消息仍按原有类型要求校验。格式修正重试会引用真正出错的字段；最终错误附带 message_type 和 next_people_type。现有失败报告未保留原始非法回复，不能确定该次实际值是否为 null。

本次同步四个文件：`im_client/communication/sim_round_state.py`、`im_client/prompts/communication.py`、`simulator/tests/test_ioa_run.py`、本指南。覆盖后运行 `python3 -B -m unittest discover -s simulator/tests -q`，再执行 `docker build -f dockerfiles/client.Dockerfile -t ioa-client:sim .` 和 `docker compose -p ioa-capture-five -f dockerfiles/compose/capture_five.local.yaml up -d --force-recreate`。只需重建客户端镜像；工具镜像、仿真服务、Server 和 Milvus 不需重建。待五个客户端 healthy 后，以原五人完整局命令开启新局。Windows 本地 52 项测试通过，未进行真实模型或 Docker 验证。

本次未调整耗时策略。wall-clock seconds 是每轮从启动 IoA 到完成会话、任务和工具审计的现实耗时，不是仿真秒。该局 t=0 至 t=3 的现实耗时为 1183.8、650.4、410.4、470.4 秒，并非逐轮递增。初始化轮必须完成双观测、五人航点和双标记；ReAct 每一步包含思考与工具选择的模型请求，IoA 还需要任务转述、结果交互和总结。每轮实际请求数、回复长度及模型服务延迟都会影响耗时；t=4 的提示历史已有 44 条消息。云端排队或网络波动只是可能因素，当前报告没有单次请求计时，不能据此判定服务端变慢。等待模型期间仿真时钟保持冻结。

流程：九候选场景 → t=0 固定选队 → 为入选者准备独立客户端 → IoA 建立五人会话 → 每秒协作、检查任务和工具调用 → 推进一步 → 保存结果、动画和对话。

选队由仿真初始化模块执行，生成名单后传给 IoA 的 team_member_names。本次没有改写 IoA Server 的 Milvus 注册和自主发现算法，也不启动九个实际客户端；四名未入选者只保留在候选表和选队记录中，不参加运动仿真或调用模型。这实现了固定选队实验边界，不能据此声称验证了九个在线 Agent 的自主动态组队。

所有入选成员都连续通信。通信窗口、消息排队、10 个场景 × 3 个通信条件留到五人完整局通过后。

固定规则：

- 以初始目标位置为圆心，筛选距离不超过 50 米的候选者。
- 固定选择 2 名 observer、1 名 leader、2 名 marker；每种功能内按距离升序，距离相同按 ID 排序。
- 不要求包含三类载体；role 与 carrier_type 是独立字段。
- 最近的入选观测者承担 t=0 的初始发现，必须在观测半径内。发现、选队、通知属于初始化，不推进仿真时间。
- 人数或功能不足直接报告场景错误，不在运行后补选。

默认入选结果：

| ID | 载体 | 功能 | 初始坐标 | 宿主机客户端端口 |
|---|---|---|---|---|
| F5_UAV_observer | UAV | 观测 | [5,40] | 5070 |
| F5_USV_observer | USV | 观测 | [9,44] | 5071 |
| F5_UUV_leader | UUV | 跟踪协调、队长 | [7,45] | 5072 |
| F5_USV_marker | USV | 标记 | [38,70] | 5073 |
| F5_UAV_marker | UAV | 标记 | [40,68] | 5074 |

其余四名候选是 F5_UUV_observer、F5_UAV_leader、F5_USV_leader、F5_UUV_marker。其中 F5_USV_leader 在 50 米外，其余因本功能名额已满而未入选。完整距离和原因写入 selection.json。

F5_ 前缀用于隔离旧三人注册信息：现有 Server 遇到同名注册会保留旧描述。首次使用新名字即可沿用 Server，不用清理数据库。以后修改已注册成员的角色描述时，应同时考虑注册信息更新，不能只改 YAML 后假定 Server 描述已更新。

保持原运动参数与折线路径 [0,40] → [12,40] → [12,55] → [101,55]。捕获规则保持原版：至少一名观测者和队长在目标 15 米内、一名标记者在 5 米内，连续满足 3 秒后由标记者明确提交有效标记请求。不要求两个标记者同时满足捕获条件。动画在捕获时停止，成功局不会显示目标走完剩余路线。

## 2. 同步文件

将更新文件按原目录合并到 Ubuntu 已成功运行的工程，保留模型 .env、原三人身份文件、数据库、日志和已有 outputs。

新增：

- simulator/team_selection.py：固定选队、校验和候选筛选记录。
- simulator/scenarios/five_team.json：九候选与 2/1/2 功能配置。
- simulator/prepare_team.py：生成五人独立配置、身份文件、客户端清单和 Compose。
- simulator/tests/test_five_team.py：选队、配置、五人 HTTP 调度和终局处理测试。
- dockerfiles/compose/simulator_five.yaml：五人仿真服务入口。
- simulator/FIVE_TEAM_IOA_GUIDE.md：本操作单。

修改：

- simulator/core.py、scenario.py：载入候选场景，运动核心只接收入选五人，结果保留选队记录。
- simulator/scripted_run.py、replay.py：无模型结果、选队记录和动画。
- simulator/service.py：导出选队记录，处理成功后另一标记者的同秒请求。
- simulator/ioa_run.py：根据实际名单生成任务、等待五人、检查双观测与双标记，读取 --clients-file。
- simulator/tests/test_replay.cjs：回放测试允许指定五名成员。
- .gitignore、.dockerignore、simulator/.dockerignore：排除新生成的本地身份和配置文件。
- simulator/README.md、CONTINUOUS_IOA_RUN.md、HTTP_IOA_GUIDE.md、UBUNTU_IOA_WIRING.md：补充新阶段入口。

本次没有修改 im_client 或工具适配器。以 Ubuntu 已通过三人整局与对话导出的版本为基础，可继续使用 ioa-client:sim、react-agent:sim；只需重建仿真服务镜像。Server、Milvus 和前端沿用现有运行方式。

## 3. 无模型验收

以下 Ubuntu 命令均在工程根目录执行。

第一步，同步源码后运行测试和纯 Python 场景：

```bash
python3 -B -m unittest discover -s simulator/tests -q
python3 -B -m simulator.scripted_run --scenario simulator/scenarios/five_team.json
```

预期：测试通过，candidate_count: 9，selected_ids 为上表五人，status: captured，capture_time: 18。生成：

- simulator/outputs/nine_candidates_five_team.html
- simulator/outputs/nine_candidates_five_team.json
- simulator/outputs/nine_candidates_five_team.selection.json

这证明运动、观测、选队和判定可工作，不代表模型协作已经通过。若浏览器拒绝直接打开 HTML，沿用此前本地 HTTP 方式浏览 outputs。

第二步，生成五人的接线文件：

```bash
python3 -B -m simulator.prepare_team
```

不调用模型、不启动容器。自动创建以下文件：

| 文件 | 用途 |
|---|---|
| simulator/service_auth_five.local.json | 五人服务身份文件，与原三人文件分开 |
| simulator/five_team.local/各成员ID.env | 每份仅有自己的 Agent token 和仿真服务 URL |
| simulator/five_team.local/selection.json | 初始化选队快照 |
| configs/client_configs/cases/capture_five.local/各成员ID.yaml | 五份完整独立客户端配置 |
| simulator/five_clients.local.json | 调度器读取的成员、角色和 5070–5074 端口映射 |
| dockerfiles/compose/capture_five.local.yaml | 五个客户端的启动文件 |

生成的 .yaml 使用 JSON 语法，现有 YAML 加载器与 Docker Compose 可以读取。各工具后端内部端口仍为 7070，客户端内部为 5050，只有宿主机映射不同。无需手改五份配置的 port 或 token。

重复执行复用内容一致的文件；内容不同会报错并保留原文件。更换场景或模型配置前，应停止五人服务并备份原生成文件，再重新生成，不能把变更混进当前实验。当前 simulator_five.yaml 固定使用 five_team.json，不要只给 prepare_team 指定另一场景而忘记同步服务端场景。

第三步，当前实验结束后停止原三人客户端和原仿真服务。以下项目名来自原操作单，若实际改过 -p 名称，请使用实际名称。

```bash
docker compose -p ioa-capture -f dockerfiles/compose/capture.yaml down
docker stop capture-observer-tool capture-leader-tool capture-marker-tool
docker compose -p ioa-simulation -f dockerfiles/compose/simulator.yaml down
docker compose -p ioa-simulation-five -f dockerfiles/compose/simulator_five.yaml config --quiet
docker compose -p ioa-simulation-five -f dockerfiles/compose/simulator_five.yaml up -d --build
docker compose -p ioa-simulation-five -f dockerfiles/compose/simulator_five.yaml ps
```

旧工具由 Docker SDK 单独创建，Compose down 不会停止它们，故另列 docker stop；某个旧工具已不存在时，对应提示可忽略。不要同时运行两个使用 ioa-simulator 网络别名和 8090 端口的仿真服务。

第四步，仿真服务 healthy 后运行无模型 HTTP 完整局：

```bash
python3 -B -m simulator.http_run --auth-file simulator/service_auth_five.local.json
```

同样应得到 capture_time: 18，并在 artifacts 中看到 selection 文件。这检查容器 HTTP 入口、权限、五人动作和结果导出。

## 4. 五人真实模型联调

确认现有 IoA Server、Milvus 正常运行，agent_network 存在，dockerfiles/compose/.env 是三人整局成功时的模型配置。

启动五个客户端：

```bash
docker compose -p ioa-capture-five -f dockerfiles/compose/capture_five.local.yaml config --quiet
docker compose -p ioa-capture-five -f dockerfiles/compose/capture_five.local.yaml up -d
docker compose -p ioa-capture-five -f dockerfiles/compose/capture_five.local.yaml ps
```

客户端各自创建工具容器；例如队长是 capture5-f5-uuv-leader-client 和 capture5-f5-uuv-leader-tool。等待五个客户端 healthy。启动错误先检查：

```bash
docker compose -p ioa-capture-five -f dockerfiles/compose/capture_five.local.yaml logs --tail=100
```

旧 ioa_check 仍是三人单时刻检查，不用于五人场景。采用下面的三轮短程检查：

```bash
python3 -B -m simulator.ioa_run --clients-file simulator/five_clients.local.json --auth-file simulator/service_auth_five.local.json --max-rounds 3 --round-timeout 1800
```

这会调用真实模型。五人自动使用每轮 max_turns=24，旧三人默认仍为 12。1800 是每轮现实等待上限，不是仿真时间，也不意味着必定等待这么久。正式实验前应冻结同一套轮次和超时设置。

三轮验收标准：

- completed_rounds: 3、time: 3、error: null、chat_export_error: null。
- status: aborted 是达到预定三轮后的主动停止，不是任务失败。
- .ioa-run.json 的 members 为五人，每轮 participants 含全部五人。
- t=0 审计至少有两名不同观测者的 observe、五人各自的 waypoint、两名标记者各自的 tag。
- t=0 若出现早于全部初始航点的标记请求，只有两名标记者之后都再次提交真实标记才算完成本轮；早期请求和重复次数仍保留在 .audit.json 中。
- 每个采样时刻两名观测者都实际调用 observe。会话、任务和审计检查通过后才推进一秒。

三轮通过后再跑完整局：

```bash
python3 -B -m simulator.ioa_run --clients-file simulator/five_clients.local.json --auth-file simulator/service_auth_five.local.json --max-rounds 0 --round-timeout 1800
```

真实模型报告 status: captured、error: null，且五人轮次记录和审计齐全，才说明五人连续通信闭环通过。18 秒是脚本基准，真实模型捕获时间不应强制等于 18 秒。正常三轮停止后可以直接开新局，每次创建新的 episode_id 和 comm_id。

如果任务或工具检查失败，先看 .ioa-run.json 的 failure_context、客户端日志及 .ioa-chat.txt，不要直接反复跑整局。若进程强退留下活动局，先重建客户端停止旧任务，再中止残留仿真：

```bash
docker compose -p ioa-capture-five -f dockerfiles/compose/capture_five.local.yaml up -d --force-recreate
python3 -B -m simulator.ioa_run --clients-file simulator/five_clients.local.json --auth-file simulator/service_auth_five.local.json --abort-active
```

之后从三轮重新检查，不要为了通过而去掉某个成员的状态检查。

## 5. 终局、输出与后续

两名标记者可能同秒先后请求标记。第一名成功后，第二名同局同秒请求返回 accepted:false、reason:already_captured、status:captured，只确认终局，不改变捕获时间、不制造第二次成功，也不追加动作审计。旧局、旧秒或非标记者请求仍拒绝。成功轮允许后一个标记动作缺省，但仍等待全部五人任务收尾。

每局继续生成轨迹 .json、.html、.audit.json、.ioa-run.json、.ioa-chat.json、.ioa-chat.txt，并新增 .selection.json。对话文件是 Server 保存的组内交换消息，不是工具后端所有私有提示与思考。捕获成功但 chat_export_error 非空时，应单独处理对话导出，不应把捕获改记为失败。

Windows 验证：45 项 Python 测试通过，覆盖旧三人回归、九选五、身份隔离、五人真实 HTTP 调度、第五人未完成时不推进、双标记终局处理；模型调用由测试替身代替。五人 HTML 已生成，播放控制通过 JavaScript 运行时检查。Windows 未运行 Docker 或五人真实模型请求。

真实五人完整局通过后，冻结本场景和参数，再实现组内预定通信窗口、关闭期排队、开放后按发送顺序交付并保留原观测时间。先检查少量受限时段，再扩展十个固定场景和三类通信条件。

需要返回旧三人流程时，停止五人客户端和仿真服务，再按原操作单启动 capture.yaml 与 simulator.yaml。旧三人场景、身份、配置和输出均保留。
