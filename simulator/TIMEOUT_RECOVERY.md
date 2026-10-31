# 逐秒 IoA 在第 10 秒超时：诊断与更新

## 五人 t=5 的已确认问题与补丁（2026-10-26）

五人完整局 `c3c922d5d10e4c9aae52497abe46399d` 的 t=5 已收到两名观测者的真实 `sim_observe` 和组内报告。队长容器日志随后出现 `KeyError: 'content'`，位置在 `communication_layer.py` 的 `_generate_response`：模型返回的 JSON 缺少必需字段，后台 `_listen_message` 任务退出。此前 `/launch_goal` 只等待结论，因此直到 1800 秒才超时。这个具体问题不能通过单纯增加 `--round-timeout` 修好。日志尚不能证明模型为何漏掉字段。

补丁改变两处：捕获模式在读取模型回复之前校验 `thought`、`message_type`、`next_people`、`content`，格式不完整时给模型一次明确的修正重试；若仍无效，后台会话写入 `round_error`，队长等待中的 `/launch_goal` 会报告错误，不再无声等到整轮超时。仿真时钟、工具审计和捕获规则不变。

将补丁中的以下文件同步到 Ubuntu 工程同名位置：

- `im_client/communication/sim_round_state.py`
- `im_client/communication/communication_layer.py`
- `simulator/tests/test_ioa_run.py`
- `simulator/FIVE_TEAM_IOA_GUIDE.md`
- `simulator/TIMEOUT_RECOVERY.md`

在 Ubuntu 工程根目录执行无模型检查、重建和递进验收：

```bash
python3 -B -m unittest discover -s simulator/tests -q
docker build -f dockerfiles/client.Dockerfile -t ioa-client:sim .
docker compose -p ioa-capture-five -f dockerfiles/compose/capture_five.local.yaml up -d --force-recreate
docker compose -p ioa-capture-five -f dockerfiles/compose/capture_five.local.yaml ps
python3 -B -m simulator.ioa_run --clients-file simulator/five_clients.local.json --auth-file simulator/service_auth_five.local.json --max-rounds 6 --round-timeout 1800
```

无需重新运行 `prepare_team`，保留 `service_auth_five.local.json`、五份 `*.env`、生成的客户端配置及数据库。只执行 `docker compose restart` 不会载入新镜像，必须 `up -d --force-recreate`。五名客户端应全部 healthy。六轮检查会调用真实模型，预期 `completed_rounds: 6`、`time: 6`、`error: null`；达到 `--max-rounds 6` 后的 `status: aborted` 是主动结束。重点检查 t=5 两名观测者和两名标记者的审计，以及第 5 秒已通过并进入第 6 秒。然后再将 `--max-rounds 6` 改为 `--max-rounds 0` 运行完整局。

若仍失败，查看新报告的 `failure_context` 和五名客户端状态。如果出现 `round_error` 或 `HTTP Error 500`，检查对应客户端日志中的 `Invalid simulation discussion response` 或 `coordination_error`，据此区分重复格式错误与模型请求失败；不要自动重发同一局的 `/launch_goal`。失败局已中止，新局需重新开始。若修正请求仍因云端响应过慢碰到 1800 秒上限，先记录每轮 `elapsed_wall_seconds` 和队长日志，再评估模型接口延迟；增加整轮上限只能作为有证据的临时联调参数，并需在正式实验前统一固定。

旧版 `TimeoutError: timed out` 只说明一个 HTTP 请求超时，不能据此断定是哪名 Agent 或哪个工具失败。`ioa_run.py` 默认给每轮 900 秒现实时间；模型调用不计入仿真时间。超时后控制器把本局中止，因此不能从第 10 秒直接续跑。

## 更新前，读取尚未重启的容器状态

在 Ubuntu 工程根目录，把下面的 `COMM_ID` 换成失败报告中的 `comm_id`。三个接口只返回轮次和任务元数据，不含目标真值或模型密钥。

```bash
COMM_ID=c98ec8466d65470b98f815edc27e83f0
for port in 5060 5061 5062; do
  echo "=== $port ==="
  curl -fsS "http://127.0.0.1:$port/simulation/round_status?comm_id=$COMM_ID"
  echo
done
```

`round_id` 是否已经到 `:10`，`pending_tasks`、`await_agents`、`concluded` 的值能区分尚未分配任务、工具仍在执行、等待队友回复和讨论已经结束。旧版报告可以从审计文件确认第 10 秒有哪些真实工具请求：

```bash
python3 - <<'PY'
import json
from pathlib import Path
p = Path('simulator/outputs/bb311f19850c4081b2ed2332a7017b0a.audit.json')
data = json.loads(p.read_text(encoding='utf-8'))
for call in data['calls']:
    if call['time'] == 10:
        print(call['agent_id'], call['operation'])
PY
```

如需进一步定位模型或工具卡住的位置，查看 `docker logs --tail 100 capture-leader-client`、`capture-observer-client`、`capture-marker-client`，以及三份配置中对应的工具容器日志。分享日志前删去 API 密钥。

## 本次代码更新

IoA 仍保留完整会话记录和审计，但在仿真模式中只把本轮及此前五轮的讨论消息送入模型。试点场景每 5 秒观测一次，因而这个窗口涵盖最近一次观测；旧任务列表不再反复加入新一轮提示。原有非仿真 IoA 的任务列表和历史处理保持不变。

调度器的新报告增加 `failure_context`：发生阶段、现实耗时、各客户端的轮次状态和出错时本秒已收到的工具调用。每个完成轮次另记 `elapsed_wall_seconds`，便于比较观测轮与普通轮的模型耗时；它不计入仿真时钟。单次 `/simulation/round_status` 网络超时现在会在本轮截止前重试，不会立刻中止整局。若是 `launch_goal` 阶段超时，通常意味着领导者的 HTTP 调用仍在等 IoA 会话结束；若是 `round_barrier` 阶段，则看 `pending_tasks` 和 `await_agents`。只在状态持续变化、确实仍有正常执行中的工具时，才考虑临时提高 `--round-timeout`；单纯增大等待时间不能修复停住的任务。

需要同步到 Ubuntu 的文件见更新包内清单。同步完成后，在工程根目录运行：

```bash
python3 -B -m unittest discover -s simulator/tests -q
docker build -f dockerfiles/client.Dockerfile -t ioa-client:sim .
docker compose -p ioa-capture -f dockerfiles/compose/capture.yaml up -d --force-recreate
python3 -B -m simulator.ioa_run --max-rounds 12
```

重建客户端也会重新创建其工具容器，清除旧局仍可能运行的任务。12 轮通过时，预定停止的报告应为 `completed_rounds: 12`、`error: null`；该停止局的 `status: aborted` 是预期行为。之后可再次重新创建三个客户端并运行完整局：

```bash
docker compose -p ioa-capture -f dockerfiles/compose/capture.yaml up -d --force-recreate
python3 -B -m simulator.ioa_run --max-rounds 0
```

如果仍超时，查看 `<episode_id>.ioa-run.json` 的 `failure_context`。它能确定下一步应修改模型/工具调用，还是处理 IoA 轮次同步。Windows 上无法验证真实 Docker 和云模型，只能先验证仿真侧和提示词窗口逻辑。
