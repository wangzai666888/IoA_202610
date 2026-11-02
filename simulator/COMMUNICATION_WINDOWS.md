# 五人同步周期通信窗口：10 种固定组合

当前版本为 **v3 授权闭环**，完整规则和四种镜像升级步骤见 [授权闭环操作说明](AUTHORIZED_CAPTURE.md)。固定十场景文件不改，实际运行规则在 reset 时统一覆盖。

新增十个冻结的随机场景：使用 `--scene 1..10` 同时选择相应场景及五客户端名单，组合仍由 `--pair 1..10` 指定。先按 [固定随机场景操作单](FIXED_SCENARIOS.md) 启动该场景的 Compose 配置；`--scene` 本身不切换正在运行的服务。下文的默认 ID 表仅适用于原 `five_team.json`；新场景请用 `--scene N --list-pairs` 查看。

本入口在现有 `five_team.json` 场景和九候选五人选队规则上加入通信窗口。按本次用户要求，受限成员由 `--pair 1` 到 `--pair 10` 明确选择，覆盖五人中任意两人的全部组合，不再随机抽取。连续通信与这 10 个组合共用同一套本地任务调度、航点规划和工具审计；不调整模型及思考强度。连续通信对照使用 `simulator.ioa_window_run --condition continuous`。

附件《IoA协同捕获实验实施方案_v1.0.md》的同步周期、两段排队及结束语义在本次落实；其中原有随机抽选 C1/C2 的实验设计，以本次明确指定 10 个组合的要求为准。附件原文不修改。

## 配置及语义

默认配置为 `simulator/scenarios/communication_windows.json`：

```json
{
  "condition": "continuous",
  "schedule": {
    "type": "periodic",
    "open_seconds": 5,
    "closed_seconds": 5,
    "period_seconds": 10,
    "phase_seconds": 0
  }
}
```

两名受限成员从仿真时刻 0 使用同一个窗口：`[0,5)` 开放、`[5,10)` 关闭、`[10,15)` 开放、`[15,20)` 关闭，依此重复至本局结束。第 5、15 秒恰好关闭，第 10、20 秒恰好开放。非受限成员一直开放，连续通信条件下五人一直开放。模型实际耗时不会改变窗口。本版只接受 5/5/10、相位 0 的统一周期，不提供错相位、随机丢包或带宽配置。

查看编号不需要启动服务，也不会调用模型：

```bash
python3 -B -m simulator.ioa_window_run --list-pairs
```

编号先将实际入选成员按观测者、队长、标记者排序，同角色按 ID 排序，再依次枚举两人组合。因此同一名单不会因配置文件行顺序或进程启动顺序改变编号。当前五人场景对应如下：

| `--pair` | 受限成员 1 | 受限成员 2 | 分组 |
| --- | --- | --- | --- |
| 1 | F5_UAV_observer | F5_USV_observer | 非队长两人 |
| 2 | F5_UAV_observer | F5_UUV_leader | 含队长 |
| 3 | F5_UAV_observer | F5_UAV_marker | 非队长两人 |
| 4 | F5_UAV_observer | F5_USV_marker | 非队长两人 |
| 5 | F5_USV_observer | F5_UUV_leader | 含队长 |
| 6 | F5_USV_observer | F5_UAV_marker | 非队长两人 |
| 7 | F5_USV_observer | F5_USV_marker | 非队长两人 |
| 8 | F5_UUV_leader | F5_UAV_marker | 含队长 |
| 9 | F5_UUV_leader | F5_USV_marker | 含队长 |
| 10 | F5_UAV_marker | F5_USV_marker | 非队长两人 |

实际选队仍由现有仿真初始化模块按功能和距离完成；调度器核对结果与五客户端清单一致，再解析组合并交给 IoA Server 建组。不会因窗口重新选队。报告自动标记 `nonleader_pair`（6 种）或 `leader_pair`（4 种），这些标签只用于归类。单独指定这两个标签而没有组合会报错，不会随机补选成员。

`--pair N` 覆盖默认配置中的连续通信设置，并自动确定分组。也可在配置中保存 `"pair_id": N`，此时移除 `"condition": "continuous"`。显式同时传入矛盾的组合和 `--condition` 会在启动前报错。旧版 `--seed`、`seed`、`closed_windows` 不再支持，避免误用随机名单或旧的一次性窗口。t=0 的初始化仍在世界静止时完成，首个周期从 t=0 开放段开始。

排队分两段：发送方关闭时，消息保存在其客户端上行队列，不能提前进入 Server 聊天记录；发送方开放后按本发送者的原始顺序进入 Server。随后仅检查接收者，下行关闭则保留该接收者的副本，其他在线成员正常接收。**消息一旦进入 Server，下行不再要求原发送者在线。** Server 记录每份副本状态并等待接收确认，重复消息/确认不会造成重复交付；新上行消息不能越过该发送者的旧积压消息。自身产生的内容在本地处理一次，不通过 Server 回送，也不引入他人的信息。

## 逐秒执行及旧消息处理

每秒按 `begin → uplink → observe → position → plan → move → authorize → tag → finish` 推进。先让所有客户端进入新仿真秒，再推进 Server 时钟并释放可交付下行，之后释放客户端可发送上行，再进行当秒采样和工具任务。同一阶段的五个客户端请求并发发出，阶段之间等待真实任务完成及当前可交付消息的接收确认。关闭窗口的上行、下行积压都不阻止时钟前进；控制响应只报告已实际发出的消息数量，不要求本地积压消息此时到达 Server。

- 两名观测者按统一采样周期（当前 3 秒）调用本地 `sim_observe`。若看到目标，在当秒 `move` 阶段直接用自己的真实观测位置更新跟随航点，保留原 `observed_at`；通信关闭也继续观测和跟随，报告留在本地上行队列。两次采样之间继续向最后观测位置移动，相同目的地不重复调用移动工具；不预测路线、不读取目标真值。两名标记者每秒调用本地 `sim_self_state` 并报告位置；队长按已收到的新鲜报告签发五秒授权，标记者仅在本人实际收到且仍有效的授权下调用 `sim_try_tag`。所有动作经原 ReAct 执行器、动作合同和真实回执校验。
- 队长只使用经组内消息交付给自己的观测。收到新观测后，规划对象明确为自己、两名标记者，以及本采样周期尚未向队长报告有效目标位置的观测者；程序校验规划名单完整且没有额外成员。已报告有效位置的观测者自行跟随，不再等待队长下发。初始只有一名观测者能看到目标时，另一名通过队长航点接近目标，之后采样看到目标便转为自主跟随。初始审计仍要求五人都有真实航点回执，其中可见观测者的回执来自本地跟随动作。
- 队长为每名观测者分别保存按原 `observed_at` 排序的最新采样（可以是未发现）和最后一次看到目标的位置。规划上下文 `observer_status` 标明采样、最后发现、原消息发送及接收时间、信息年龄和本周期是否收到采样；`latest_sample=null` 表示从未收到，采样内 `target_position=null` 表示那次采样未看到。未收到本周期报告不能等同于本周期未看到。新未见报告也触发重新规划；迟到旧报告不能覆盖更新的采样状态，但可补充尚不存在的最后发现记录。
- 航点只能使用队长收到的所有正观测中 `observed_at` 最新的位置与原时刻，程序会校验。即使最新正观测之后收到未见报告，历史位置仍可用于引导，但不表示目标此刻仍在该处，也不表示任何人此刻仍能看到目标。信息年龄的自然增长不会单独触发模型调用；没有新报告时继续既有任务。
- 观测者以自己的最新本地采样判断是否可见，不用收到的同伴报告冒充本地观测。有本周期有效本地观测时优先自主跟随，即使队长因报告未送达而补发了引导航点，也记录 `observer_guidance_ignored` 并采用本地观测。最新采样未发现目标时可以执行当秒有效的队长引导；没有引导则继续已接受的航点，到达后停留。这是采样间隔内的跟随策略，不保证任何速度、路线下都不会失去覆盖。
- 队长没有新观测信息时不调用规划模型。除自主跟随的观测者外，无新航点指令的成员继续最后一个有效航点，到达后停留。此策略同时用于本入口的连续通信和十种组合；旧 `simulator.ioa_run` 的集中调度入口未改，不应作为新策略的连续通信对照。
- 队长下发的航点仍须经过组内消息交付才能执行（队长自己的航点使用本地回声）。未获回执的目的地会以当前秒合同重新发送；成员对相同目的地只返回原真实回执，不重复设置航点。新的模型计划取代尚未执行的旧计划。观测者自主跟随的航点直接来自自己的本地观测，不需要这次通信往返。
- 第 6 秒观测在第 10 秒收到时，保留 `sent_at=6`、`delivered_at=10`、原 `proposal_id`，工具回执仍为 `observed_at=6`。若是受限观测者，其 `server_received_at=10`；若观测者一直在线而接收者受限，则 `server_received_at=6`。接收方保存历史信息，不把旧坐标改写成当前真值。
- 第 5 秒的航点指令若第 10 秒才收到，记录 `expired_command`，不会自动改成第 10 秒动作。队长须根据已收到的信息签发新合同；历史消息仍保存在日志中，且保留原观测时间。
- 控制接口传递时钟、角色、阶段及任务完成元数据，不向 Agent 提供目标真值或转述其他成员的观测。工具回执校验失败、模型请求失败或审计缺动作时停止该局，不把缺失动作视作成功。

结束时停止消息转发。若捕获发生在关闭窗口内，上行未发送消息和下行未交付副本均保留在导出记录中，不在结束后补发。捕获时间由仿真器实际受理有效标记决定，不等待队长知情；另外记录队长是否已获知及获知时刻，未收到时为 `null`。

## 部署本次代码

本次必须重建 Server、客户端、ReAct 工具和仿真器四种镜像，再重建相应容器。使用 [授权闭环操作说明](AUTHORIZED_CAPTURE.md#部署和运行) 中的完整命令。窗口协议已升级为 v3，旧 v2 Server/客户端会在 reset 和模型调用前被拦截。不能再沿用“只重建客户端”的旧升级方法。

## 先短测，再指定组合跑完整局

短测默认改为 25 轮，即最多执行 t=0 到 t=24，可以覆盖首次关闭和重新开放。达到计划轮数会返回 `aborted` 且 `error=null`；若提前捕获、逃脱或超时则立即结束。当前场景可能在第 20 秒前就捕获，不能为了看到恢复而延长已结束的局。

```bash
python3 -B -m simulator.ioa_window_run --condition continuous --max-rounds 25
python3 -B -m simulator.ioa_window_run --pair 1 --max-rounds 25
python3 -B -m simulator.ioa_window_run --pair 2 --max-rounds 25
```

完整局使用 `--max-rounds 0`，每条命令只运行所选的一局。把 `--pair` 的值换成表中的 1—10 即可；不要同时启动两个调度器操作同一个仿真服务：

```bash
python3 -B -m simulator.ioa_window_run --condition continuous --max-rounds 0
python3 -B -m simulator.ioa_window_run --pair 1 --max-rounds 0
python3 -B -m simulator.ioa_window_run --pair 10 --max-rounds 0
```

默认使用 `simulator/five_clients.local.json`、`simulator/service_auth_five.local.json`、仿真端口 8090、Server 端口 7788，均可用参数覆盖。`--round-timeout 1800` 是单秒所有阶段及通信确认的现实耗时上限，不是关闭窗口长度。

异常中止后先检查日志与该局报告。正常退出会关闭本局窗口会话，下一局可以复用客户端。强制杀进程、重启 Server 或客户端后不支持从内存队列断点恢复：应重建客户端、终止遗留运行局，再从 t=0 新开一局。原终止命令仍适用：

```bash
python3 -B -m simulator.ioa_run --clients-file simulator/five_clients.local.json --auth-file simulator/service_auth_five.local.json --abort-active
```

## 输出及检查重点

每局仍有轨迹、工具审计、HTML 回放、`.ioa-run.json`、`.ioa-chat.json`、`.ioa-chat.txt`，另增加 `<episode_id>.communication.json`：

- `.ioa-run.json`：新增 `experiment_rules` 和逐条 `authorizations` 时间线（签发、入队、送达、到期、有效授权尝试及失败原因）；另记录 `profile.pair_id`、分组、实际名单与受限名单、固定周期、每秒真实动作及两段队列计数、捕获时间、`leader_capture_known_at`、`leader_informed_at_end`、现实耗时、错误及导出状态；`experiment_snapshot` 保存相关源码、提示词、配置和场景的 SHA-256，以及模型配置。该指纹用于比对文件，不证明运行中的容器已更新，也不保存环境文件中的密钥。
- `.communication.json` 的 `transport.messages` 仅包含实际进入 Server 的消息；`deliveries` 记录每名远端接收者的 `queued/inflight/acknowledged`、`sent_at/server_received_at/delivered_at`。`sender_sequence` 表示发送者原始顺序，`sequence` 表示进入 Server 的顺序。
- `local_events[成员].uplink_messages` 保存该成员产生的全部消息、原轮次、消息 ID、上行发送时间及 `queued/sent` 状态，结束时未发送消息也在此处。`sent` 表示客户端已提交 WebSocket，是否已进入 Server 还应核对 `transport.messages`。
- `local_events[成员].events` 区分 `generate/uplink_enqueue/uplink_send`、本地处理、阶段进度、`tool_start/tool_complete`、接收和过期指令事件。自主跟随记录 `observer_follow`（含原采样时间与目的地），本地可见时忽略队长引导记录 `observer_guidance_ignored`。工具结果以原工具审计及执行器回执为准。
- 原 `.ioa-chat.txt` 按产生顺序显示群消息，不代表每名成员在那个时刻已经收到；交付时间以 `.communication.json` 为准。

上下行队列分别在客户端和 Server 进程内维护。Server 每秒完成和结束时保存已入站消息的快照，调度器结束时导出双方记录。支持正常运行期间不丢消息；不支持进程崩溃后的队列续跑或网络断开后的自动重试。

验收重点：第 5/10/15/20 秒边界、两个受限成员同步、关闭期间上行不进入 Server、开放成员仍可通信、下行不重复检查发送者、本地动作正常、历史观测时间不被改写、旧航点不执行、无重复交付、整局工具审计通过。真实模型结果以你运行后的报告为准。

不调用模型的本地回归检查：

```bash
NO_PROXY=127.0.0.1,localhost no_proxy=127.0.0.1,localhost python3 -B -m unittest discover -s simulator/tests -q
```

测试覆盖连续通信和全部 10 个组合、关闭窗口内捕获并保留两段队列、队长受限、历史观测、旧命令过期、幂等性和缺失真实回执时停止推进。另用临时测试配置降低队伍速度，在保持冻结捕获规则的前提下检查多次恢复，不修改正式场景。消息和模型使用替身，Server/客户端端点函数、接收入口、ReAct 校验及仿真 HTTP 工具使用实际代码。

自主跟随回归还覆盖：关闭期间本地观测和移动继续、相同目的地及阶段重试不重复执行、不能用同伴报告代替本地视野、有效本地观测优先于队长引导、失去本地视野后可接受引导。另使用临时测试场景将一名入选观测者放在观测半径之外，验证真实工具链下从队长引导切换到自主跟随，正式场景文件不改。

队长状态回归覆盖最新采样为未见、从未收到报告、保留历史发现、新未见报告触发规划、迟到旧报告与原始时间、禁止选择较旧正观测，以及仅时间流逝不重复请求模型。

## 代码入口

| 文件 | 责任 |
| --- | --- |
| `common/communication_windows.py` | 10 种组合固定编号、同步周期、仿真时钟与逐接收者下行队列 |
| `common/types/communication.py` | 消息的原始产生时刻与交付元数据承载字段 |
| `im_server/app.py` | 建立窗口会话、转发/排队/确认、推进窗口时钟和导出 |
| `im_client/communication/window_capture.py` | 客户端上行队列、本地阶段与回声、真实回执、历史观测及旧命令拒绝 |
| `im_client/communication/communication_layer.py` | 窗口消息接收分支，复用既有工具执行入口 |
| `im_client/main.py` | 本地窗口会话初始化、阶段触发、状态/日志控制接口 |
| `simulator/ioa_window_run.py` | 组合选择/查看、统一调度、版本检查、工具审计、时钟推进和输出 |
| `simulator/scenarios/communication_windows.json` | 默认连续通信及统一 5/5/10、相位 0 周期 |
| `simulator/tests/test_communication_windows.py` | 无模型费用的机制与整局回归测试 |
