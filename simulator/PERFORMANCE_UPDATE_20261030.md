# 捕获仿真调用链优化（2026-10-30）

本次针对重复航点任务、长报告和模型往返。作用于启用 `SIM_URL` 的捕获流程；
普通 IoA 案例保留原任务转述、两阶段 ReAct 和模型总结。

## 修改内容

1. 五人配置生成器、现有五人队长配置、三人队长配置和逐秒目标一致规定：
   全员在 t=0 设置自身航点；之后仅在目的地变化时分配航点任务。
   队长没有“每秒重设自身航点”的义务。运行时提示进一步说明这条规则，
   覆盖 Milvus 中可能保留的旧角色描述，因此无需清空注册数据库。
   这是对分配策略及提示歧义的修正，没有把重复动作伪装成新的真实回执。
   模型是否仍重复分配，应由真实运行的审计验证。
2. 捕获任务直接转交当前收到的分配消息，不再调用模型转述。
   新增 `im_client/communication/capture_execution.py` 校验接收者和轮次，
   并要求分配消息写全本次工具、参数及观测时间。工具执行器只操作当前成员，
   不继承“协调整个队伍”的职责，也不从历史自行补全缺失坐标。
3. 捕获 ReAct 每步只有一次工具选择请求，不再单独生成 thought JSON。
   工具仍实际执行、绑定 episode/time、校验角色及回执，并将结果输入下一步。
   普通“一个动作，再 submit_task”任务的成员侧请求由常见的
   1 次转述 + 4 次 ReAct 减为 2 次；失败恢复或多动作任务可能需要更多请求。
   不再向捕获模型提供不能执行真实动作的 `subtask_solver`。
4. 捕获提交提示要求一句话，取消约 400 单词总结。
   任务结果正文保留完整原始回执，不再重复任务描述；任务描述仍在协议字段和任务管理器中。
   回执 JSON 去掉缩进，只有不可信模型说明在传入组内对话时限制到 240 字符。
   uid、Agent、轮次、坐标、observed_at、accepted 等原始回执字段不裁剪。
5. 轮末不再请求模型生成第二篇长总结，改为程序生成简短轮次状态。
   `audit: pending_controller_verification` 表示仍须通过原调度器的屏障和工具审计，
   不表示已经捕获或已经审计通过。

仿真步长、观测周期、运动参数、捕获条件、每秒标记要求、五人等待条件、
工具审计、角色身份及 stale episode/time 拒绝规则均沿用原实现。
没有改动模型名称、`SIM_REASONING_EFFORT`、轮次超时或 ReAct 步数上限。
没有改为失联或异步通信策略，也没有让控制器代替 Agent 执行动作。

## 修改文件

- `im_client/communication/capture_execution.py`（新增）
- `im_client/communication/communication_layer.py`
- `im_client/agents/react/react_agent.py`
- `im_client/agents/react/capture_guard.py`
- `simulator/ioa_run.py`
- `simulator/prepare_team.py`
- `configs/client_configs/cases/capture_five.local/F5_UUV_leader.yaml`
- `configs/client_configs/cases/capture/leader.yaml`
- `simulator/tests/test_capture_evidence.py`
- `simulator/tests/test_capture_performance.py`（新增）

## 验证

```bash
python3 -B -m unittest discover -s simulator/tests -q
```

74 项无模型测试通过。新增检查覆盖：当前任务直接转交、拒绝旧轮次和错误接收者、
每步一次模型请求、文字回复不能直接完成、角色工具限制、多工具结果、完整回执与短说明、
轮末保留未完成任务、不调用总结模型，以及非捕获流程回归。

新增五人集成测试使用真实通信处理方法、任务管理器、ReAct 循环和本机 HTTP 仿真器，
仅替换模型及 Docker/数据库基础设施。t=0..5 共 26 个工具任务、52 次工具侧模型替身请求；
t=0 和 t=5 更新航点，t=1..4 继续原航点且每秒双标记，逐轮通过真实工具审计。
这验证调用链和请求数量，不预测真实模型的选择或云端耗时。

若本机测试受代理影响，可仅为 localhost 添加 `NO_PROXY`/`no_proxy` 后重跑。
不要因本机端口代理错误而调整生产模型地址。

## 在 Ubuntu 使更新生效

当前运行结束后，在工程根目录重建两类镜像并重建五个客户端：

```bash
docker build -f dockerfiles/tool_agents/react_capture.Dockerfile -t react-agent:sim .
docker build -f dockerfiles/client.Dockerfile -t ioa-client:sim .
docker compose -p ioa-capture-five -f dockerfiles/compose/capture_five.local.yaml up -d --force-recreate
docker compose -p ioa-capture-five -f dockerfiles/compose/capture_five.local.yaml ps
```

两个 Dockerfile 已复制所需源码目录，无需修改。客户端启动时会重建各自工具容器。
只修改宿主机源码或只 restart 旧容器，不足以替换镜像内的代码。
不需要重新生成 token 或五人清单，也不需要重建仿真服务、IoA Server、Milvus 或前端。
`ioa_run.py` 由宿主机执行，因此使用当前工程目录运行即可。

等待客户端 healthy 后，建议先检查六轮，覆盖 t=0 初始化与 t=5 首次后续采样：

```bash
python3 -B -m simulator.ioa_run --clients-file simulator/five_clients.local.json --auth-file simulator/service_auth_five.local.json --max-rounds 6 --round-timeout 1800
```

正常六轮检查结束应为 `completed_rounds: 6`、`time: 6`、`error: null`，
`status: aborted` 是计划停止。核对：

- t=0 双观测、五人航点、双标记及动作顺序齐全。
- t=1..4 目标航点未改变时，不再出现队长反复设置相同航点；每秒仍有双标记。
- t=5 能获取新观测，按新的任务参数更新所需航点，再执行标记。
- `.ioa-chat.txt` 仍有执行器真实回执，轮末为简短 JSON 状态。
- 比较 `.ioa-run.json` 的 `elapsed_wall_seconds`，并记录相同模型、思考强度和部署版本。

六轮通过后，以相同参数运行完整局：

```bash
python3 -B -m simulator.ioa_run --clients-file simulator/five_clients.local.json --auth-file simulator/service_auth_five.local.json --max-rounds 0 --round-timeout 1800
```

本次没有启动 Docker 服务或调用付费模型。真实模型完整局稳定性和实际加速幅度由上述运行确认。
