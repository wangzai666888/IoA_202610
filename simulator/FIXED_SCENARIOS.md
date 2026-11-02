# 十个固定随机场景：全图选队与通信窗口

**当前授权闭环版本：**请先按 [授权闭环操作说明](AUTHORIZED_CAPTURE.md) 升级四种镜像。十份场景原文件保持不变，`ioa_window_run` 统一启用 5/5 同步窗口、3 秒观测和授权捕获规则；文件内的旧规则参数不代表该入口实际生效的配置。

场景已生成并保存在 `simulator/scenarios/fixed_10/scene_01.json` 至 `scene_10.json`。本次改变九名候选者的初始位置；目标仍沿 `[0,40] → [12,40] → [12,55] → [101,55]` 运动，其他速度、半径、采样周期、捕获保持和最大时间参数沿用原场景。连续通信和所有 pair 均读取同一份保存的 JSON，不会在 reset 时重新抽样。

## 随机化与选队

- 每次抽样对九名候选者的 x、y 分别独立地在 `[0,100]` 米均匀取值；不设置最小间距、不强行分散、不固定入选 ID。
- 仅当三名观测者都看不到初始目标时，重抽整个九人布局。每个场景记录种子、抽样次数和因初始未发现而拒绝的次数。最终场景集合以“初始至少一人发现”为条件，不能称为无条件均匀样本。
- 全图按距离选择最近的 2 名观测者、1 名队长、2 名标记者，同距按 ID 排序。新场景使用 `selection.radius=null`，取消 50 米限制；旧 `five_team.json` 保留历史配置供复现。
- 不运行捕获策略来挑选场景，不因为失败、过早成功或通信影响小而重抽。未入选的四人保留在场景和选队记录中，不启动其客户端。
- `manifest.json` 保存文件 SHA-256、入选 ID 和每个场景的全部十种 pair。运行器在 reset 前及 reset 后核对实际服务场景 ID、哈希和名单，避免相同五人但坐标不同的场景被混用。

| `--scene` | 场景文件 | `scenario_id` | 种子 | 抽样次数 |
| --- | --- | --- | --- | --- |
| 1 | `scene_01.json` | `random_fixed_01` | 20261031 | 1 |
| 2 | `scene_02.json` | `random_fixed_02` | 20261032 | 3 |
| 3 | `scene_03.json` | `random_fixed_03` | 20261033 | 3 |
| 4 | `scene_04.json` | `random_fixed_04` | 20261034 | 1 |
| 5 | `scene_05.json` | `random_fixed_05` | 20261035 | 1 |
| 6 | `scene_06.json` | `random_fixed_06` | 20261036 | 1 |
| 7 | `scene_07.json` | `random_fixed_07` | 20261037 | 3 |
| 8 | `scene_08.json` | `random_fixed_08` | 20261038 | 7 |
| 9 | `scene_09.json` | `random_fixed_09` | 20261039 | 12 |
| 10 | `scene_10.json` | `random_fixed_10` | 20261040 | 2 |

查看实际入选名单、检查冻结文件，不需要服务或模型：

```bash
python3 -B -m simulator.fixed_scenarios list
python3 -B -m simulator.fixed_scenarios verify
python3 -B -m simulator.ioa_window_run --scene 1 --list-pairs
```

将最后一条的 `1` 换成 `2` 至 `10`，查看相应场景的 pair。编号仍按“观测者、队长、标记者，同角色按 ID 排序”枚举，不能沿用原固定五人名单对应的 ID 表。

## 首次部署

以下命令在工程根目录运行。先让正在运行的旧局正常结束。若需要主动终止旧入口的活动局，使用原命令：

```bash
python3 -B -m simulator.ioa_run --clients-file simulator/five_clients.local.json --auth-file simulator/service_auth_five.local.json --abort-active
```

新场景与旧服务复用宿主机端口 8090、5070–5074，应先停止原五客户端和原五人仿真服务。以下项目名沿用仓库部署指南；如你实际部署时用了其他项目名，请使用实际项目名：

```bash
docker compose -p ioa-capture-five -f dockerfiles/compose/capture_five.local.yaml stop
docker compose -p ioa-simulation-five -f dockerfiles/compose/simulator_five.yaml stop
```

IoA Server、Milvus、网络 `agent_network` 继续使用原部署。新仿真服务增加了场景身份核对接口，本次必须重建仿真镜像：

```bash
docker build -f simulator/Dockerfile -t ioa-simulator:five simulator
python3 -B -m simulator.prepare_fixed_scenarios
```

当前工作目录中的十份运行配置已生成。`prepare_fixed_scenarios` 再执行会校验并复用一致的配置和凭据，适合将源码与固定场景复制到云服务器后准备当地路径。它从当前 `configs/client_configs/cases/capture_five.local/` 复制同 ID 或同角色的模型设置，保留模型、温度、步数、镜像等配置，为新 ID 更新身份、角色说明和工具容器槽位；不会回退到旧生成脚本的默认模型。模型 API 环境继续从 `dockerfiles/compose/.env` 读取，不写入场景或公开报告。

已生成配置若被手动更改，生成器会拒绝覆盖。若要有意更改模型或搬迁工程路径，先保存并移走旧的 `simulator/fixed_runtime/` 和 `configs/client_configs/cases/fixed_scenes.local/`，再准备新的配置；相应的通信对照需要在同一套模型配置下重跑。

当前 v3 授权闭环版本必须同步重建 Server、客户端、工具与仿真镜像，具体命令见上述授权闭环说明。客户端配置通过挂载加载，切换场景时需要重建容器。

## 选择场景并运行条件

只需将 `SCENE` 设为 1–10 中的一个整数。该变量同时决定启动哪个场景服务和运行哪个客户端名单。

```bash
SCENE=1
printf -v SCENE_TAG '%02d' "$SCENE"
docker compose -p ioa-fixed-scenes -f "simulator/fixed_runtime/scene_${SCENE_TAG}/compose.yaml" up -d --force-recreate
docker compose -p ioa-fixed-scenes -f "simulator/fixed_runtime/scene_${SCENE_TAG}/compose.yaml" ps
```

固定场景的仿真容器会只读挂载宿主机的 `simulator/replay_template.html`。旧容器首次采用此挂载时，须在当前实验结束后重新执行上面的 Compose `up -d --force-recreate`。挂载生效后，后续模板修改会在下一次导出回放时被读取，无须再重建容器。历史 HTML 不会仅因模板文件变化而自动更新，已保存的逐秒 JSON 可用于重新渲染，无须重跑模型。

等待仿真服务和五个客户端均 healthy，然后查看组合并运行：

```bash
python3 -B -m simulator.ioa_window_run --scene "$SCENE" --list-pairs
python3 -B -m simulator.ioa_window_run --scene "$SCENE" --condition continuous --max-rounds 0
python3 -B -m simulator.ioa_window_run --scene "$SCENE" --pair 1 --max-rounds 0
python3 -B -m simulator.ioa_window_run --scene "$SCENE" --pair 2 --max-rounds 0
python3 -B -m simulator.ioa_window_run --scene "$SCENE" --pair 10 --max-rounds 0
```

每条命令是一局，应等待前一局退出。`--max-rounds 0` 跑到捕获、逃脱或超时；短测可设为 `25`。如要依次跑当前场景的十个 pair，可在连续通信完成后执行：

```bash
for PAIR in {1..10}; do
  python3 -B -m simulator.ioa_window_run --scene "$SCENE" --pair "$PAIR" --max-rounds 0 || break
done
```

切换到下一个场景时，例如 `SCENE=2`，先结束当前局，再重新执行上面的 `printf`、Compose `up --force-recreate` 和 `ps`，随后执行新场景的条件。对 3–10 同理。**仅改变运行器的 `--scene` 不会自动重启容器或切换仿真服务**；错误搭配会在模型调用前被身份/哈希核对拦截。

所有新场景共用 Compose 项目 `ioa-fixed-scenes` 和五个客户端/工具槽位，场景数据库、凭据和配置分别存放；每次切换都重建五个客户端，工具容器随之使用新场景的 Agent ID 与 token。不要同时运行两个场景的调度器或另起一个固定场景 Compose 项目。

如需主动终止当前新场景的活动局：

```bash
python3 -B -m simulator.ioa_run --clients-file "simulator/fixed_runtime/scene_${SCENE_TAG}/clients.json" --auth-file "simulator/fixed_runtime/scene_${SCENE_TAG}/auth.local.json" --abort-active
```

## 文件和结果

| 路径 | 内容 |
| --- | --- |
| `simulator/scenarios/fixed_10/scene_XX.json` | 冻结的九人坐标、目标路线、规则、独立 ID 和随机化记录 |
| `simulator/scenarios/fixed_10/manifest.json` | 十场景哈希、入选五人、全部 pair |
| `simulator/fixed_runtime/scene_XX/clients.json` | 本场景成员、端口、配置文件路径、预期场景哈希 |
| `simulator/fixed_runtime/scene_XX/pairs.json`、`selection.json` | 组合与完整选队记录 |
| `simulator/fixed_runtime/scene_XX/compose.yaml` | 配套仿真服务和五客户端 |
| `simulator/fixed_runtime/scene_XX/auth.local.json`、`*.env` | 本地私有凭据，排除 Git/Docker 构建上下文 |
| `configs/client_configs/cases/fixed_scenes.local/scene_XX/` | 对应五名成员的配置 |
| `simulator/outputs/scene_XX/` | 本场景各局结果、动画、审计、对话、通信队列 |

每局使用独立 episode_id，不覆盖同场景其他条件。`.ioa-run.json` 保存实际 `scenario_id`、`scenario_sha256`、生成种子、入选名单、受限名单及实际配置快照；轨迹 JSON 和 HTML 内嵌记录同样包含场景哈希和生成元数据。源码快照表示工作目录内容，不能代替容器镜像更新检查。

场景 JSON 和凭据只挂载到仿真服务；Agent 不获得这些文件。运行器的场景核对仅用于控制侧验证，不向队长提供目标路线或候选者坐标。

重新生成同一批冻结文件（通常无需执行）：

```bash
python3 -B -m simulator.fixed_scenarios generate
```

默认种子和原模板未变时，生成字节一致；若保存的文件不同则拒绝覆盖。不要在不同 pair 之间改种子、坐标或路线。

本地验证，无付费模型请求：

```bash
NO_PROXY=127.0.0.1,localhost no_proxy=127.0.0.1,localhost python3 -B -m unittest discover -s simulator/tests -q
```

测试检查十场景可复现和初始发现筛选、全图角色配额、配置/凭据对应、场景混用拒绝，以及十份实际名单在连续通信、含队长受限和两标记者受限条件下通过真实仿真 HTTP 工具、接收处理与审计。模型输出、持久化和 Socket 传输使用替身；真实模型的完成时间和捕获表现以实际运行结果为准。
