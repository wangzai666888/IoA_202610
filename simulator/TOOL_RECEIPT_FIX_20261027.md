# 五人连续通信：防止仅提交文字而未执行工具（2026-10-27）

## 问题与证据

失败局 `7d3e2e61720e40f3a9e321555749a3a0` 在 t=11 缺少 `F5_USV_marker:tag`。
队长已分配该任务；USV 标记者返回的文字声称执行完毕，却没有实际回执；队长随后生成了
`accepted=false` 的完整回执。仿真器审计只有队长的两次 waypoint 和 UAV 标记者的 tag。
因此不能把队长的总结当成 USV 已执行动作的证据，也不能删除审计检查。

源码中有两个与此相关的缺口：

- ReAct 接受任意非空 `submit_task` 结论，没有检查本任务是否实际执行了动作。
- 思考阶段也提供工具，收到的工具调用却会被清除，没有交给执行器。

现有记录不足以确定这次 USV 在工具后端内部走了哪条路径；本补丁同时封住这两个缺口。

## 本次文件清单

| 路径 | 变化 | 用途 |
|---|---|---|
| `im_client/agents/react/react_agent.py` | 修改 | 记录本任务真实工具回执；拦截无动作的 submit；思考阶段禁用工具选择，兼容接口仍返回工具调用时交给同一个执行器执行 |
| `im_client/agents/react/react_agent_adapter.py` | 修改 | 检查 HTTP 状态、任务身份与回执，原样将回执放在任务报告前面 |
| `im_client/agents/react/capture_guard.py` | 新增 | 不依赖第三方包的回执检查与报告格式化 |
| `simulator/tests/test_capture_evidence.py` | 新增 | 真实 ReAct 执行循环配合模型替身、真实 HTTP 仿真器回归测试 |
| `simulator/TOOL_RECEIPT_FIX_20261027.md` | 新增 | 本说明与 Ubuntu 更新步骤 |

压缩包直接包含上述相对路径，没有额外顶层目录，不含密钥、本地身份文件、输出结果或其他历史补丁。
它是当前已经同步过前几次修复的工程的增量补丁，不是完整工程。

## 修改后的完成条件

开启 `SIM_URL` 时，每个下发给 ReAct 的捕获工具任务必须包含真实动作。
`sim_observe`、`sim_move_to`、`sim_try_tag` 至少一项返回与当前任务 episode/time 匹配的有效回执，才允许提交。
读取自身状态、引用历史回执、`subtask_solver` 或单纯文字声明不能解锁提交。
`sim_try_tag` 返回 `accepted=false` 是有效回执；它表示没有捕获，不是工具执行失败。
`already_captured` 和未观察到目标的合法回执同样正常处理。

如果模型过早调用 submit_task，执行器向模型返回拒绝原因，任务在原有 ReAct 步数上限内继续。
不延长步数上限，不推进仿真时间，不由调度器代替 Agent 标记。步数耗尽仍无回执则明确失败。
本任务回执以 uid 隔离，不允许前一任务、前一秒或另一成员的回执混用。

这是任务级的最低动作检查，不是对模型任务语义的完整证明。
“指定成员是否调用了指定工具、动作顺序是否正确”仍由原 `tool_audit` 检查；任何缺项依然阻止推进一秒。
以后如需纯讨论或纯状态查询任务，应单独设计其完成条件，不能借它满足本阶段的真实动作任务。

工具后端现在返回带 `ioa-capture-tool-result-v1` 标识的结构化执行结果。
客户端把执行器的原始回执放在模型总结之前，使 `.ioa-chat.json/.txt` 可以直接查看真实结果。
原有仿真场景、轨迹、捕获规则、观察周期、每轮超时、对话长度和模型参数未调整。

## Ubuntu 更新步骤

1. 等当前运行结束，保留失败局输出。如需保留旧工具容器日志，请在重建客户端前导出。
2. 备份两个被修改的 Python 文件。将压缩包传到 Ubuntu，解压到工程根目录并覆盖同名文件。
   下例假设压缩包在 `~/Downloads`；若云盘下载位置不同，只改压缩包路径。

```bash
cd ~/ICLR_IoA_202610
unzip -o ~/Downloads/five_agent_tool_receipt_fix_20261027.zip -d .
python3 -B -m unittest discover -s simulator/tests -q
```

本地测试预期为 65 项通过，无模型调用费用。测试用的本机 HTTP 端口自动分配，不使用现有 Docker 仿真服务。

3. 必须同时重建工具镜像与客户端镜像。两个 Dockerfile 已经复制整个 ReAct 源码目录，无需修改 Dockerfile。

```bash
docker build -f dockerfiles/tool_agents/react_capture.Dockerfile -t react-agent:sim .
docker build -f dockerfiles/client.Dockerfile -t ioa-client:sim .
docker compose -p ioa-capture-five -f dockerfiles/compose/capture_five.local.yaml up -d --force-recreate
docker compose -p ioa-capture-five -f dockerfiles/compose/capture_five.local.yaml ps
```

等待五个客户端 healthy。客户端启动时会删除各自的旧工具容器并按新镜像重建，单纯 restart 不会替换镜像。
本次无需重建仿真服务、IoA Server 或 Milvus，无需重新生成五人配置和 token。
若只更新客户端而未更新工具镜像，会明确报 `capture backend returned no executor evidence; update the tool image`。

4. 先用一轮确认两端协议与回执接线，检查新局 `.ioa-chat.txt` 中含有“执行器记录的真实工具回执”和
   `tool_receipts`，且能找到两名标记者的真实 `sim_try_tag` 记录。

```bash
python3 -B -m simulator.ioa_run --clients-file simulator/five_clients.local.json --auth-file simulator/service_auth_five.local.json --max-rounds 1 --round-timeout 1800
```

预期 `completed_rounds: 1`、`error: null`；短程检查正常主动停止时 `status: aborted`。
若提示仍有活动局，先确认旧调度进程已停止，再沿用现有 `--abort-active` 流程清理活动局。

5. 一轮通过后运行新完整局，不续跑旧失败局。

```bash
python3 -B -m simulator.ioa_run --clients-file simulator/five_clients.local.json --auth-file simulator/service_auth_five.local.json --max-rounds 0 --round-timeout 1800
```

完整局以 `status: captured`、`error: null` 和完整审计为验收标准；不强制真实模型捕获时间等于脚本的 18 秒。
新日志中的 `[CAPTURE_EVIDENCE] ... submit_task rejected` 表示程序阻止过一次无回执完成；后续
`tool=sim_try_tag receipt_recorded` 表示实际工具返回了有效回执。发生拦截后成功执行属于有界恢复，不是伪造成功。

## 已做验证与限制

Windows 上 65 项无模型测试通过。新增测试覆盖无工具提交、只查自身状态、HTTP 错误、旧任务回执、
正常 false 回执、already_captured、工具调用在思考阶段返回、旧版工具镜像拒绝，以及真实 HTTP 仿真器 t=11 的补执行。
后者先模拟 USV 仅提交文字，再完成真实 tag，确认只产生一次 tag 且时钟始终停在 11。
已有三人、五人、固定选队、动作时序和捕获测试继续通过。

Windows 没有运行 Docker 或调用付费模型；五人真实模型整局是否通过，需要按上述 Ubuntu 流程验证。
本次不扩展通信窗口实验；正式对比实验应统一使用同一执行器版本和失败判据。
