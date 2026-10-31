# 从最小仿真到 IoA 连续通信：实施顺序与操作说明

2026-10-26 更新：本文保留早期分阶段设计。三人真实模型整局已通过，九候选五人固定选队与连续通信接线已加入；本阶段部署以 [五人实施操作单](FIVE_TEAM_IOA_GUIDE.md) 为准。

## 1. 推荐方案与当前完成边界

采用方案 2，并在接模型之前增加一次 HTTP 脚本验证：

1. 三人纯 Python 场景和动画（已完成）。
2. 同一个核心增加 HTTP 服务，通过 HTTP 脚本验证同一局；再迁到 Ubuntu 验证 Docker（HTTP 代码已完成，Docker 文件已准备）。
3. 注册三角色工具，先验证一个冻结时刻的 IoA 协作，再编写逐秒实验调度器，跑通三角色连续通信。
4. 增加九候选的固定选队模块，选出两观测者、一队长、两标记者；连续通信通过后再接通信窗口。
5. 校准并冻结场景，运行 10 场景 × 3 条件。

这样可分别定位环境规则、HTTP 接口、容器网络、工具调用、IoA 协作、选队和通信窗口的问题。五人/选队并不难，但提前增加它们不能解决 IoA 与仿真之间的时钟和工具接入问题。

更新：第 6 节的工具注册、环境传递、三份角色配置和独立 capture Compose 已经写入工程；新增 simulator.prepare_ioa 和 simulator.ioa_check 自动准备身份并检查第 0 秒的真实工具调用。请按 [Ubuntu 接线与验收操作单](UBUNTU_IOA_WIRING.md) 部署，不需要再手工粘贴第 6 节片段。第 7 节完整逐秒调度仍是后续工作；本地没有 Docker 和实际模型联调结果。

## 2. 已新增文件

| 文件 | 作用 |
|---|---|
| simulator/service.py | 单进程 HTTP 服务；使用现有 CaptureEnvironment，串行处理状态变更 |
| simulator/http_client.py | 控制器与 Agent 的 HTTP 客户端；只用标准库 |
| simulator/http_run.py | 用真实 HTTP 重放原脚本策略，不调用模型 |
| simulator/ioa_tools.py | 可供 ReAct 注册的四个本地工具适配函数及三角色工具描述 |
| simulator/Dockerfile | 仅打包仿真模块，不安装模型、Milvus 或绘图库 |
| dockerfiles/compose/simulator.yaml | 在 agent_network 上运行仿真服务，绑定结果输出目录 |
| simulator/tests/test_service.py | 本机 HTTP、权限、时钟、旧指令、回放和工具适配验证 |

输出文件按 episode_id 命名，避免同一场景多次运行互相覆盖。仿真服务不会自动随现实时间推进，不需要在模型调用期间暂停一个后台计时器。

服务暂时一次只维护一局；运行中不能直接 reset 覆盖，可以由控制器明确 abort。进程重启会丢失未结束局的内存状态，已导出的回放保留在挂载目录中。当前按顺序跑实验，不并发启动多局。

## 3. 现在在 Windows 上怎么验证

以下命令均在现有 IoA 工程根目录执行，需要 Python 3.10 或更新版本，不需要 Docker。

第一次生成本地实验身份文件：

    python -B -m simulator.service --init-auth

生成 simulator/service_auth.local.json，其中有一个控制器 token 和每个成员独立的 token。文件已被 Git 和 Docker 构建上下文忽略；已有文件不会被覆盖。这只是仿真接口身份，不是模型 API 密钥。

终端 A：

    python -B -m simulator.service

终端 B：

    python -B -m simulator.http_run

预期输出 captured、capture_time 为 18、trace_frames 为 19，并给出本局 HTML/JSON 文件名。文件位于 simulator/outputs/，打开 HTML 查看回放。这个验证脚本是可信测试程序，持有各测试角色的 token，以复现原策略；它不代表 IoA 已参与协作。

运行检查：

    python -B -m unittest discover -s simulator/tests -v

退出服务用终端 A 的 Ctrl+C。若调试中断留下了一局 running，先用控制器 abort，或者在本地调试时重启服务。不要把进程重启当成正式实验失败记录的替代方式。

## 4. 迁到 Ubuntu 后运行 Docker

本轮需要迁移 simulator/ 目录和 dockerfiles/compose/simulator.yaml。保留 Ubuntu 上已经可用的 IoA 配置；仿真服务不依赖其模型密钥。若没有迁移本地身份文件，在 Ubuntu 工程根目录首次执行：

    python3 -B -m simulator.service --init-auth

如果该文件已经存在，直接复用，不重复初始化。随后执行：

    docker network inspect agent_network >/dev/null 2>&1 || docker network create agent_network
    docker compose -p ioa-simulation -f dockerfiles/compose/simulator.yaml config
    docker compose -p ioa-simulation -f dockerfiles/compose/simulator.yaml up -d --build
    docker compose -p ioa-simulation -f dockerfiles/compose/simulator.yaml ps

再从 Ubuntu 宿主机执行：

    python3 -B -m simulator.http_run

应再次得到第 18 秒捕获。检查日志：

    docker compose -p ioa-simulation -f dockerfiles/compose/simulator.yaml logs --tail=100 simulator

地址分清两个位置：

- Windows/Ubuntu 宿主机访问本机服务：http://127.0.0.1:8090。
- agent_network 内的 IoA 工具容器访问：http://ioa-simulator:8090。

工具容器内的 localhost 指向工具容器自身。Compose 只将 8090 绑定到宿主机回环地址，容器之间仍通过 agent_network 通信。若宿主机开着 Windows 原生服务，先停止它，以免迁移后的同一台机器上发生端口冲突。

Compose 将宿主机 simulator/outputs 挂到 /app/outputs；生成的动画可从宿主机打开或复制回 Windows。身份文件仅挂到仿真服务，不能整体挂给 Agent；每个 Agent 只拿自己的 token。

本轮未在 Docker 中构建或启动，以上是 Ubuntu 上的部署验收步骤。

## 5. 已实现的 API 与权限

所有业务接口用 Authorization: Bearer <token>。Agent 身份由 token 绑定，动作请求不允许额外传入 agent_id 来操作其他成员。

| 方法和路径 | 调用者 | 用途 |
|---|---|---|
| GET /health | 任意 | 进程健康状态，不含仿真真值 |
| POST /control/reset | 控制器 | 初始化一局，返回 episode_id、时刻和成员名单 |
| GET /control/status | 控制器 | 状态、时刻、周期、成员角色，不含目标坐标 |
| GET /control/audit | 控制器 | 运行中读取已成功执行的观测、航点和标记调用，用于逐秒轮次屏障 |
| POST /control/step | 控制器 | 推进恰好一个仿真秒 |
| POST /control/finish | 控制器 | 在时间上限处理完当秒标记请求后，判定超时 |
| POST /control/abort | 控制器 | 明确中止异常局，并记录原因 |
| GET /control/result | 控制器 | 本局结束后获取完整结果；运行中不可获取 |
| GET /agent/state | 任一成员 | 自身状态、episode_id、仿真时刻 |
| GET /agent/observation | 观测者 | 最近的合法本地采样及 observed_at |
| POST /agent/waypoint | 任一成员 | 设置自身持续航点 |
| POST /agent/tag | 标记者 | 提交当前时刻的标记请求 |

三角色客户端另提供 `GET /simulation/round_status?comm_id=...`，返回本轮消息数、结论状态、待执行及失败任务和等待回复成员；这不是仿真服务接口，也不包含目标真值。只在配置了 SIM_URL 的客户端启用。

step、finish、tag 的请求体为：

    {"episode_id": "本局返回的ID", "expected_time": 0}

waypoint 另加 point：

    {"episode_id": "本局返回的ID", "expected_time": 0, "point": [10, 40]}

abort 另加非空 reason。reset 的请求体为空对象。

expected_time 是本次动作所处的执行时刻。模型在第 5 秒开始的任务，不能等到控制器已经推进到第 6 秒后还悄悄生效；旧任务返回 HTTP 409。401 表示身份错误，403 表示角色无权限，400 表示参数错误。客户端不会自动重试写操作，因为请求超时不代表服务端没有执行。

通信窗口扩展时，expected_time 不能与 observed_at 或消息发送时刻混淆：在第 20 秒从队列合法交付的一条第 10 秒消息，应保留其原观测/发送时间，并在第 20 秒的新执行任务上下文中处理。这里的过期检查只用于防止已经关闭的执行轮次继续写入，不能用来丢弃约定排队交付的旧消息。

## 6. 下一阶段：把四个工具接到 IoA

本节接线已落实到文件，操作命令以 UBUNTU_IOA_WIRING.md 为准。下面保留实现说明供核对。

建议先用三个 ReAct 工具后端，区分功能、描述和权限。此前已经接受“功能异构先于底层框架异构”；这一步不需要同时调试 AutoGPT、Open Interpreter、ReAct 三套工具协议。若后续要研究三种底层智能体实现，再单独替换。

四个适配函数已经在 simulator/ioa_tools.py：

- sim_self_state：自身状态。
- sim_observe：观测者采样。
- sim_move_to：自身航点。
- sim_try_tag：标记者请求。

返回值是 JSON 字符串，符合当前 ReAct 使用 ToolResponse(observation=...) 的格式。工具函数从 SIM_URL 和 SIM_AGENT_TOKEN 读取连接信息，不读取场景、回放或控制器凭证。

已应用以下接线修改：

### 6.1 工具镜像

在 dockerfiles/tool_agents/react.Dockerfile 的其他 COPY 指令之后、最后的 ENTRYPOINT 之前已新增：

    COPY simulator/__init__.py simulator/http_client.py simulator/ioa_tools.py /app/simulator/
    RUN python -m simulator.ioa_tools --output-dir /app/simulator/tool_configs

只复制 HTTP 客户端和工具适配器，不把 core.py、场景路线或输出文件复制给 Agent。生成的是 JSON 格式工具描述，现有 yaml.safe_load 可以读取。

在 im_client/agents/tools/__init__.py 的映射建立后启用仿真专用映射（SIM_URL 非空时）：

    import os
    if os.environ.get("SIM_URL"):
        from simulator.ioa_tools import TOOL_FUNCTIONS
        TOOL_MAPPING = dict(TOOL_FUNCTIONS)
        BROWSER_TOOLS_MAPPING = {}

### 6.2 让环境变量进入真正的工具容器

当前 im_client/agents/base.py 的 AgentAdapter.create 在 ReAct 分支中使用 Docker SDK 创建工具容器，并显式列出了 environment。给这个列表增加：

    f"SIM_URL={os.environ.get('SIM_URL', '')}",
    f"SIM_AGENT_TOKEN={os.environ.get('SIM_AGENT_TOKEN', '')}",

只在外层 IoA 客户端的 Compose 中添加变量是不够的，动态启动的工具容器不会自动继承它们。

### 6.3 三份独立客户端配置

已在 configs/client_configs/cases/capture/ 下建立三份完整配置。三份 server.port 都为 7788，tool_agent.port 都为 7070；外层客户端在 capture.yaml 中映射为 5060/5061/5062。共享模型与 rag 默认项，角色差异如下：

| 字段 | observer.yaml | leader.yaml | marker.yaml |
|---|---|---|---|
| comm.name 与 tool_agent.agent_name | UAV_observer | UUV_leader | USV_marker |
| tool_agent.container_name | capture-observer-tool | capture-leader-tool | capture-marker-tool |
| tool_agent.tools_config | /app/simulator/tool_configs/observer.json | /app/simulator/tool_configs/leader.json | /app/simulator/tool_configs/marker.json |
| tool_agent.image_name | react-agent:sim | react-agent:sim | react-agent:sim |

三份配置保留 discussion_only: false、support_nested_teams: false，初步将 tool_agent.max_num_steps 设为 6。描述字段同时写入 comm.desc 和 tool_agent.desc：

- 观测者：调用本地观测工具，将坐标和 observed_at 通过 IoA 组内消息发给队长；按收到的指令设置自己的航点。
- 队长：只使用观测者实际报告的坐标，不预测未来路线；分配三个成员的航点，并调用工具设置自己的航点。不能替成员直接调用其接口。
- 标记者：依据 IoA 中收到的指令设置自身航点，提交本地标记请求，报告真实 accepted 返回值。

容器名和注册名必须分别唯一。载体标签与功能仍是两个字段，三人示例并不规定以后三种载体与三种角色一一对应。

### 6.4 三个客户端服务

已新增 dockerfiles/compose/capture.yaml，每个服务沿用现有客户端的模型 env_file、configs 挂载、Docker socket 挂载以及 agent_network，分别使用自己的数据库目录和角色身份文件；分别传入：

    CUSTOM_CONFIG=configs/cases/capture/observer.yaml
    SIM_URL=http://ioa-simulator:8090
    SIM_AGENT_TOKEN=该成员自己的token

队长与标记者分别换成对应配置及 token，宿主机端口可分配 5060、5061、5062，容器端口均为 5050。不要将包含所有 token 的文件作为三个客户端共同的 env_file，也不要给客户端控制器 token。保留已有 ioa-server、Milvus 和前端，不需要重建它们。

修改完成后，在 Ubuntu 工程根目录构建新标签，避免覆盖当前能工作的镜像：

    docker build -f dockerfiles/client.Dockerfile -t ioa-client:sim .
    docker build -f dockerfiles/tool_agents/react_capture.Dockerfile -t react-agent:sim .

新 capture Compose 使用 ioa-client:sim；其启动的工具容器使用 react-agent:sim。捕获实验镜像通过 react_capture.Dockerfile 使用精简依赖，不安装原始 requirements.txt 中的 GitHub 版 python-miio。原始 react.Dockerfile 保留给其他 ReAct 场景。

## 7. 先验收“一个时刻”，再编写完整 IoA 调度器

更新：三角色连续通信的逐秒调度已新增 simulator/ioa_run.py、每轮状态接口和工具审计检查。请按 [逐秒 IoA 调度操作单](CONTINUOUS_IOA_RUN.md) 部署和验收。下文保留最初的设计说明；九候选五人队伍与通信窗口尚未加入。

下面的单时刻流程已经封装为 python3 -B -m simulator.ioa_check。运行前按 Ubuntu 操作单完成部署；脚本会调用真实模型、核对工具审计、主动 abort 导出记录，返回 tool_check_passed。它不会推进仿真，也不会实现下面描述的完整 ioa_run 调度器。

控制器先 reset 获得 episode_id，并保持 t=0，不调用 step。在队长客户端 5061 的 /launch_goal 提交固定成员名单：

    {
      "goal": "本轮是仿真时刻0，episode_id为<实际ID>。请观测者实际调用sim_observe并报告坐标和observed_at；队长根据该报告分配航点；每人实际调用sim_move_to设置自身航点；标记者实际调用sim_try_tag并报告返回值。只处理本轮，不等待位置移动，不预测目标轨迹。采用同步任务分配，所有任务完成后再结束本轮讨论。",
      "team_member_names": ["UAV_observer", "UUV_leader", "USV_marker"],
      "team_up_depth": 0,
      "max_turns": 12,
      "skip_naming": true
    }

max_turns 和 max_num_steps 是联调起点，需要记录，正式比较前统一冻结。此处 goal 不包含目标坐标；目标信息必须从观测者工具经过 IoA 到达队长。各成员都使用本轮给定的 episode_id、expected_time=0。

验收依据是：服务中产生了各成员的真实 waypoint_set 和 tag_request 事件；t 仍为 0；成员的工具调用结果确实经过 IoA 消息返回。自然语言说“已经移动”“已经捕获”不能当作成功证据。单时刻检查结束后可由控制器 abort("single-tick integration check") 导出该局日志。

完整实验调度器应是新增的 simulator/ioa_run.py，而不是把 http_run.py 的即时集中转发直接当成 IoA：

    reset，取得 episode_id，固定三名成员
    创建一个 IoA 会话，保存返回的 comm_id
    对每个仿真时刻 t：
        为该轮建立执行上下文 episode_id、t
        发起/续接有界 IoA 讨论与同步工具任务
        等待本轮工具任务实际结束，关闭本轮执行上下文
        查询服务状态；若已捕获/逃脱，结束
        t 达到时限时调用 finish，否则调用 step 推进一秒
    读取控制器结果与自动生成的 HTML/JSON

首轮联调可每秒安排一次有界协作轮次，以便标记者每秒有机会请求标记；模型/API 耗时不改变 t。这种方式调用成本较高，只用于先跑通。后续再统一改为按观测周期协调、每秒由本地执行器处理移动与标记，并冻结一致的调用调度规则。

续接会话采用 /launch_goal 已支持的 comm_id、cont_input，例如：

    {
      "goal": "协同捕获",
      "comm_id": "<上一轮返回的ID>",
      "cont_input": {"sender": "user", "content": "进入仿真时刻1；episode_id为<实际ID>。按本轮上下文处理任务，保留观测时间。"},
      "max_turns": 12
    }

以上流程还需要解决当前 IoA 代码中的两个实际接线点，不能只循环发送上述请求：

1. communication_layer.py 的 _generate_response 目前用整个会话历史长度比较 max_turns。复用 comm_id 时，第二轮起不能仍把 12 当作“本轮12条”。应在每轮开始记录消息数基线，并在仿真模式下按本轮增量计数；同时用程序级调用预算/超时处理保证有界，而非只依赖提示词要求结束。
2. /launch_goal 的返回表示讨论有 conclusion，不自动证明异步工具任务全部结束。首轮使用同步任务，调度器还应检查本轮任务完成状态；TaskManager 已有 TO_START、IN_PROGRESS、COMPLETED 和按状态筛选接口，可增加一个仿真轮次状态接口供控制器查询。旧任务未结束时不能正常推进一秒。异常/超时应明确中止并记录，不能冒充目标逃脱。

这是为什么建议先三人联调：这里的难点在任务生命周期与时钟接口，与候选人数无关。

加入通信窗口后，不能要求所有失联成员都完成通信才能推进世界；应改成每帧只处理当前可交付事件，未交付消息留在队列，时间继续推进。本轮连续通信验证不等同于窗口调度器已经实现。

## 8. 九候选五人队伍与路线何时加入

三人 IoA 连续通信稳定后，场景结构再拆为 candidates（九名候选）与 selected_members（五名入选）。不能直接把九名、含三名协调者的名单塞入当前核心，因为运行队伍要求唯一队长。

固定选队模块独立于运动核心：以初始发现位置计算 50 米候选范围，保留首次发现者，在功能内按距离排序并以 agent_id 打破距离并列，选两名观测者、一名协调者、两名标记者。先选队再设置受限名单，不要求队伍含三类载体。功能不足的预设场景在实验前判为不合法，不根据运行结果补选。

目标当前完整路线为 (0,40) → (12,40) → (12,55) → (101,55)；成功局只播放到第18秒，所以看起来只走了开头一段。核心已支持任意非零长度折线段，不限定转角为90度。后续可以只改场景 JSON 中的地图大小、target.route、初始位置、速度和时限；保持起点在边界、中间航点在区域内、终点在区域外即可。现在保留已知可成功的场景，便于比较每次接线前后的行为。
