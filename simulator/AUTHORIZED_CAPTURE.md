# 通信窗口授权闭环首版（authorized_capture_v1）

本版统一使用 `simulator.ioa_window_run` 运行连续通信和全部十个 pair。原 `ioa_run`、脚本基准入口保留旧规则用于历史回归，不能作为本版连续通信对照。

## 冻结规则

唯一规则源是 `simulator/experiment_rules.py`，窗口配置是 `common/communication_windows.py` 和 `simulator/scenarios/communication_windows.json`。

| 项目 | 首版固定规则 |
| --- | --- |
| 通信窗口 | 5 秒开放、5 秒关闭，周期 10 秒，两名受限者相位均为 0；`[0,5)` 开放，`[5,10)` 关闭 |
| 观测 | t=0、3、6、9…采样；观测者自主跟随自己的正观测，失去观测时执行已收到的有效引导 |
| 自身位置报告 | 两名标记者每秒各调用一次 `sim_self_state`，报告真实坐标及读取时刻；关闭时排队 |
| 信息新鲜度 | 签发时，目标正观测与标记者位置报告各自年龄均须 `0≤age≤5` 秒 |
| 签发距离 | 两份已收到报告中的坐标距离 `≤5m`；不外推运动、不读取隐藏真值 |
| 授权期限 | `[issued_at, issued_at+5)`；签发当秒可用，第 5 秒到期；排队和重发不续期 |
| 续签 | 同一标记者的上一授权到期前不重复签发；到期后必须重新检查当时已收到的信息 |
| 本地执行 | 本人实际收到且仍有效的授权下，每秒最多提交一次标记；已收授权在关窗期间仍可用，无授权则跳过 |
| 物理捕获 | 本标记者实际距离目标 `≤5m`，独立连续计数达到 3，且本次请求有已送达的有效授权 |
| 三秒计时 | 保留仿真离散口径：t=0 计数为 0；每次推进后，在 t=1、2、3…的帧末检查位置，每个合格帧计 1；任一不合格帧立即归零。第一次合格帧 t=k，则 k、k+1、k+2 三帧合格后可标记；不做帧间亚秒碰撞推算 |
| 支持条件 | 不要求队长或观测者在 15m 内；允许不同观测者接替报告；两名标记者的计数不能相加 |

计数与授权独立：标记者可以先在范围内累计三秒，再收到授权并立即提交。授权过期不清空物理计数，但禁止再提交；离开范围才清空该标记者计数。标记者看不到自己的物理计数或目标真值，因此有效授权下仍可能失败，不能保证消除失败请求。

最新采样与历史正观测分开保存。某观测者的新“未见目标”会阻止使用该观测者更早的正观测签发新授权；另一观测者的最新正观测仍可用。新负观测不会远程撤销已经签发的授权，首版通过固定到期时间限制它；不另加撤销协议。历史正观测仍可指导搜寻航点，但不能冒充当前可见状态。

授权是队长客户端根据已知报告执行的固定规则决策，航点规划继续由现有 IoA 模型完成。没有额外加入自由文本授权谈判或模型自由选择授权期限。这让所有条件共享可复现的决策门槛。

## 每秒处理顺序

`begin → uplink → observe → position → plan → move → authorize → tag → finish`

1. 所有客户端进入本秒，清理到期状态；Server 开放可交付下行，客户端开放可发送上行。历史消息保留原始时刻。
2. 到观测周期时，两名观测者调用本地传感工具并报告。两名标记者每秒读取自身状态并报告。组内消息均经过同一窗口队列。
3. 队长根据已收到的观测规划自己、标记者及需要引导的观测者的航点。已有目的地不重复执行；迟到旧轮航点仍不能当作本秒命令。
4. `move` 只设置目的地，世界尚未前进。观测者优先使用自己的本地观测跟随。队长据本秒已有信息执行授权决策；授权包含身份、信息来源及原时刻、签发时刻、到期时刻、估计距离和规则版本。
5. 控制器从 Server 获取**标记者已确认接收**的授权交付记录，登记到仿真器。它只核对交付证据，不决定授权，不向 Agent 传递位置或目标真值。只入队或只发出但未确认的授权不能登记。
6. 标记者仅选择本人的有效已收授权，通过原 ReAct 执行器提交请求。授权 ID 从动作合同绑定到工具调用，模型不填写、更换或延长它。仿真器先核验授权身份、实际交付与期限，再独立裁决真实距离和三秒计数。
7. 真实反馈经消息队列报告给队长。审计检查本秒观测、位置报告及任何标记的有效授权；没有授权时允许没有 `tag`。本秒完成后才推进世界一秒。

关闭期间执行已收到的持续航点和有效授权；队长自身关闭时也可以根据已有信息作决定，但新授权必须留在上行队列，不能提前让标记者获知。首版保留 FIFO 和旧命令管理，不把任务改为跨轮合同。

例如 t=4 签发并送达的授权在 t=5–8 关闭期间仍可用，t=9 停止；t=5 签发、t=10 才送达的授权已经过期，不能使用。所有时间均为仿真秒。

## 保留场景与结果追溯

十份 `simulator/scenarios/fixed_10/scene_XX.json`、manifest、候选者坐标、目标路线、选队、pair 对应关系和客户端模型配置均保留。reset 时通过受控规则覆盖设置采样周期、标记半径、保持秒数和授权/支持开关，不改场景字节及其哈希。

因此文件中仍能看到旧 `observation_period=5`；**本入口实际运行的是 3**。实际规则写入服务状态、轨迹 JSON/HTML 的 `experiment_rules` 和有效 `config`，以及 `.ioa-run.json` 的 `experiment_rules`。对比结果必须同时核对原场景哈希、规则版本、窗口 profile、模型快照，不能仅凭相同 scenario_id 混用历史结果。

`.ioa-run.json` 的 `authorizations` 给出每条授权的签发、上行入队、Server 接收、实际送达、固定到期时刻、上下行等待时长、结束前是否已过期，以及每次标记的有效性和失败原因。独立的 `.communication.json` 中，`local_events` 包含每秒决策原因、位置/观测接收、过期、跳过标记、尝试和反馈；同文件的 `transport` 保存完整消息及逐接收者交付证据。授权到期时局已结束的情况不补造过期事件，使用期限及 `expired_by_end` 判断。

常见原因：`no_fresh_positive_sample`、`no_fresh_marker_position`、`reported_distance_outside_radius`、`previous_authorization_unexpired`；物理失败为 `marker_out_of_range`、`marker_hold_incomplete`。授权校验失败会保留原因并使工具合同/逐秒审计失败，不把异常请求当作正常已授权动作。

捕获发生时立即结束，未交付反馈不在结束后补发；`leader_capture_known_at` 可以为空。它与真实 `capture_time` 分开记录。

动画保留以当前目标为圆心的观测圈和 5m 标记圈。本版不再绘制 15m 支持圈，帧信息显示各标记者连续处于标记范围的计数；旧规则回放仍保留旧支持圈含义。

## 部署和运行

本次涉及 **Server、客户端、ReAct 工具、仿真器四种镜像**，协议升级为 v3。只编辑宿主机源码或重启旧容器不会更新镜像内代码；旧窗口 v2 进程会被入口拒绝。先让正在运行的旧局结束，并保存所需聊天导出；下面命令不更换模型、场景或凭据。

在工程根目录构建：

```bash
docker build -f dockerfiles/server.Dockerfile -t ioa-server:latest .
docker build -f dockerfiles/client.Dockerfile -t ioa-client:sim .
docker build -f dockerfiles/tool_agents/react_capture.Dockerfile -t react-agent:sim .
docker build -f simulator/Dockerfile -t ioa-simulator:five simulator
```

使用现有 Server 的 Compose 项目名重建该服务，不启动 basic.yaml 中其他示例 Agent：

```bash
IOA_SERVER_PROJECT="$(docker inspect ioa-server --format '{{index .Config.Labels "com.docker.compose.project"}}')"
docker compose -p "$IOA_SERVER_PROJECT" -f dockerfiles/compose/basic.yaml up -d --no-deps --force-recreate Server
```

以上沿用仓库的 Compose 部署；若 Server 原先并非通过此 Compose 启动，应沿用它原有的启动参数和环境，不能使用空项目名。

选择固定场景，例如场景 1，重建其仿真服务和五客户端；客户端会重新创建配套工具容器，加载新 `react-agent:sim`：

```bash
SCENE=1
printf -v SCENE_TAG '%02d' "$SCENE"
python3 -B -m simulator.fixed_scenarios verify
docker compose -p ioa-fixed-scenes -f "simulator/fixed_runtime/scene_${SCENE_TAG}/compose.yaml" up -d --force-recreate
docker compose -p ioa-fixed-scenes -f "simulator/fixed_runtime/scene_${SCENE_TAG}/compose.yaml" ps
python3 -B -m simulator.ioa_window_run --scene "$SCENE" --list-pairs
```

待服务和五客户端 healthy 后，顺序运行：

```bash
python3 -B -m simulator.ioa_window_run --scene "$SCENE" --condition continuous --max-rounds 0
python3 -B -m simulator.ioa_window_run --scene "$SCENE" --pair 2 --max-rounds 0
```

运行全部 pair：

```bash
for PAIR in {1..10}; do
  python3 -B -m simulator.ioa_window_run --scene "$SCENE" --pair "$PAIR" --max-rounds 0 || break
done
```

切换场景时将 SCENE 改为 2–10，重新设置 SCENE_TAG 并重建场景 Compose，再运行同样的条件。单独改变 `--scene` 不会切换正在运行的仿真器。已有运行配置和私有凭据无需重新生成。

无真实模型的验证：

```bash
NO_PROXY=127.0.0.1,localhost no_proxy=127.0.0.1,localhost python3 -B -m unittest discover -s simulator/tests -q
```

整局测试使用真实本地 HTTP 仿真工具、IoA 接收/路由及审计，模型和 Socket 使用替身；不代表真实模型整局时长或成功率。每秒位置报告会增加工具任务，取消未授权标记会减少工具任务，实际墙钟耗时需由后续真实仿真测量。
