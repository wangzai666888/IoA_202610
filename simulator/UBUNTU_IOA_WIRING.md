# Ubuntu：动画访问、三角色工具接线与第 0 秒验收

本文件对应 2026-10-24 的本地修改。工程根目录指包含 simulator、im_client、configs、dockerfiles 的 ICLR_IoA_202610 目录。以下命令均在 Ubuntu 的该目录执行。

已实现：三角色工具注册、角色权限、动态工具容器身份传递、客户端配置、Compose、独立身份文件生成、第 0 秒联调与工具审计。

更新：三人逐秒 IoA 调度见 [三人操作单](CONTINUOUS_IOA_RUN.md)。九候选五人固定选队及连续通信接线已加入，见 [五人实施操作单](FIVE_TEAM_IOA_GUIDE.md)；本文其余部分保留三人操作。通信窗口仍待实现。第 18 秒是无模型脚本基准，不是模型接入后的固定结果。

## 1. “第 18 秒捕获”但 HTML 访问被拒绝

捕获输出说明仿真运行完成。若浏览器显示“对该文件的访问请求被拒绝”，通常是 file:// 路径权限或浏览器沙箱访问范围问题；还没有证据证明是页面 JavaScript 故障。Docker 输出的 root 所有权本身不一定会阻止读取，仍需看实际权限与父目录。

推荐从 Ubuntu 宿主机用 HTTP 查看回放，不修改浏览器安全配置。新开一个终端：

    python3 -m http.server 8091 --bind 127.0.0.1 --directory simulator/outputs

保持终端运行，在同一台 Ubuntu 的浏览器访问：

    http://127.0.0.1:8091/

点击本次 http_run 输出的 artifacts.html 对应文件。HTTP 服务输出使用 episode_id 文件名，不一定叫 pilot_success_boundary_route.html。这里的 8091 是回放浏览端口，8090 是仿真 API，二者不同。目录必须是 simulator/outputs，不要把整个工程或身份文件目录作为网页根目录。

如果通过 HTTP 能播放，就不必修改 HTML。该方法使用同一个 HTML 和内嵌数据，只改变浏览器读取方式。

如果目录或文件仍然拒绝访问，在工程根目录检查：

    pwd
    ls -ld simulator simulator/outputs
    ls -l simulator/outputs/*.html
    namei -l simulator/outputs

用 namei -l 加上具体 HTML 路径可进一步检查文件和每一级父目录。若只有 outputs 内的文件权限有问题，且这些就是当前账号的实验输出，可仅修复输出目录：

    sudo chown -R "$(id -u):$(id -g)" simulator/outputs
    find simulator/outputs -type d -exec chmod u+rwx {} +
    find simulator/outputs -type f -exec chmod u+rw {} +

再重新启动上面的本地 HTTP 服务。不要对整个工程执行 chmod -R 777；若工程位于 /root 等当前用户无法遍历的位置，把所需回放复制到自己家目录的可读目录再提供 HTTP 服务。不要用 sudo 启动浏览器。

Ubuntu 若是远程无桌面主机，127.0.0.1 指 Ubuntu 自身，不能直接在 Windows 使用该地址访问远程服务；可以把 HTML 复制回 Windows 打开。本操作单默认你是在 Ubuntu 本机浏览器查看。

## 2. 从 Windows 同步哪些文件

新增：

- configs/client_configs/cases/capture/observer.yaml
- configs/client_configs/cases/capture/leader.yaml
- configs/client_configs/cases/capture/marker.yaml
- dockerfiles/compose/capture.yaml
- dockerfiles/tool_agents/react_capture.Dockerfile
- im_client/agents/react/requirements_capture.txt
- simulator/prepare_ioa.py
- simulator/ioa_check.py
- simulator/tests/test_prepare_ioa.py
- simulator/UBUNTU_IOA_WIRING.md

修改：

- .dockerignore：根构建上下文排除仿真身份文件及输出。
- dockerfiles/tool_agents/react.Dockerfile：加入工具适配器并生成三份工具描述。
- im_client/agents/base.py：把当前角色的 SIM_URL、SIM_AGENT_TOKEN 传入其工具容器。
- im_client/agents/tools/__init__.py：仿真模式只启用仿真工具映射。
- im_client/agents/react/react_agent.py：仿真模式检查调用工具是否出现在该角色配置中。
- im_client/communication/communication_layer.py：修复固定名单组队时联系人判断的一处括号错误；没有改动会话轮次调度。
- simulator/service.py：保存成功观测读取、航点设置、标记请求的工具审计，不改变运动规则。
- simulator/tests/test_service.py：补充单时刻检查、虚报成功、超时中止验证。
- simulator/README.md
- simulator/HTTP_IOA_GUIDE.md

保留 Ubuntu 上的 dockerfiles/compose/.env、simulator/service_auth.local.json、已有输出和数据库；本次没有替换模型 API 地址/密钥。不要用 Windows 的凭证覆盖 Ubuntu 当前服务凭证。

如果 Ubuntu 上 communication_layer.py 还有 Windows 未包含的修改，合并时只需修正这一行：

    if item.get("name") not in self.agent_contact:

原错误为 item.get("name" not in self.agent_contact)。当前 Windows 文件保留了已有的时间线等修改。

## 3. Dockerfile 和端口的确切位置

react.Dockerfile 的新增 COPY/RUN 位于其他 COPY 之后、最后 ENTRYPOINT 之前，已经写好，不要再追加一遍。只复制 __init__.py、http_client.py、ioa_tools.py，不给工具容器复制场景、仿真真值或控制器身份。

| 配置或服务 | 观测者 | 队长 | 标记者 |
|---|---|---|---|
| comm.name / agent_name | UAV_observer | UUV_leader | USV_marker |
| server.hostname | ioa-server | ioa-server | ioa-server |
| server.port | 7788 | 7788 | 7788 |
| tool_agent.port | 7070 | 7070 | 7070 |
| 工具容器名 | capture-observer-tool | capture-leader-tool | capture-marker-tool |
| 外层客户端容器端口 | 5050 | 5050 | 5050 |
| 外层客户端宿主机端口 | 5060 | 5061 | 5062 |

不同容器拥有各自的网络空间，内部端口相同不冲突。ReAct 工具当前通过容器名:7070 访问，没有把工具端口映射到宿主机；不要把 YAML 的 tool_agent.port 改成 5060/5061/5062。

角色和载体仍是独立概念。这三个人名只是当前 pilot 场景的对应关系，不规定正式实验中所有 UAV 都必须是观测者。

## 4. Ubuntu 部署顺序

先确认原 ioa-server 与 Milvus 正常，并在 agent_network。它们保持原来已成功的启动方式。新 capture.yaml 不重复创建 Server、Milvus 或前端。

    docker ps --format 'table {{.Names}}\t{{.Status}}\t{{.Ports}}'
    docker network inspect agent_network

首次创建身份文件的命令是 python3 -B -m simulator.service --init-auth；你已完成第 4 步且现有文件可用，直接复用即可。

生成三份互相独立的角色身份文件：

    python3 -B -m simulator.prepare_ioa

只打印文件名，不打印 token。相同内容重复运行无害；内容不一致会报错而不会覆盖。三个文件为 simulator/observer.local.env、leader.local.env、marker.local.env，每份只有自己的 Agent 身份，没有控制器 token。

确认 dockerfiles/compose/.env 仍是当前成功连接并行智算云的原文件。新客户端继续使用该文件里的模型连接变量，三份角色配置使用 Qwen3.8-Flash。不要手工把身份 token 粘贴进公共 .env。

重建仿真服务，以载入新增工具审计：

    docker compose -p ioa-simulation -f dockerfiles/compose/simulator.yaml up -d --build
    python3 -B -m simulator.http_run

这一步仍应是 capture_time: 18。它同时验证重建后运动规则没有改变。已有运行中实验先完成或明确中止后再重建。

构建两个新标签。捕获实验使用独立的 react_capture.Dockerfile；原 react.Dockerfile 会安装 GitHub 开发版 python-miio，当前要求 Python 3.11，而原镜像为 Python 3.10.13。捕获工具既不使用该智能家居依赖，也不需要 Playwright、RAG 等完整工具集。

    docker build -f dockerfiles/client.Dockerfile -t ioa-client:sim .
    docker build -f dockerfiles/tool_agents/react_capture.Dockerfile -t react-agent:sim .

启动角色客户端：

    docker compose -p ioa-capture -f dockerfiles/compose/capture.yaml config --quiet
    docker compose -p ioa-capture -f dockerfiles/compose/capture.yaml up -d
    docker compose -p ioa-capture -f dockerfiles/compose/capture.yaml ps

config --quiet 用于验证 Compose，避免把展开后的身份变量打印到终端。第一次启动还会经 Docker SDK 创建三个工具容器；无需自己 docker run 工具容器。捕获镜像构建时会自动执行一次导入检查，不调用模型；依赖安装、拉镜像和真实模型接入只在 Ubuntu 验收。

等待三个客户端 healthy，并检查日志：

    docker compose -p ioa-capture -f dockerfiles/compose/capture.yaml logs --tail=100
    docker ps --format 'table {{.Names}}\t{{.Status}}'

三个工具容器都应在运行。以下命令从队长客户端检查仿真网络和三个工具 HTTP 服务，不调用模型：

    docker exec capture-leader-client python -c "import urllib.request as u; print(u.urlopen('http://ioa-simulator:8090/health',timeout=5).read().decode()); [print(role,u.urlopen(u.Request('http://capture-'+role+'-tool:7070/keep_alive',data=b'',method='POST'),timeout=5).read().decode()) for role in ('observer','leader','marker')]"

若有一个失败，先查对应容器日志，如：

    docker logs --tail=100 capture-observer-tool
    docker logs --tail=100 capture-leader-client

工具连接拒绝、缺依赖、注册/Milvus 失败都应先在本步解决，不要直接进入循环调用模型。

## 5. 执行一个冻结时刻的真实 IoA 联调

不要同时运行 http_run、其他控制器或再次启动同一个检查。运行：

    python3 -B -m simulator.ioa_check

此命令会通过三个客户端及工具容器调用真实模型，可能产生模型费用。它会：

1. 检查三个外层客户端 API 是否就绪。
2. reset 创建一局，固定成员名单，取得本局 episode_id。
3. 向队长 5061/launch_goal 发起一次协作，要求同步工具任务。目标坐标不放进控制器提示词。
4. 保持仿真 t=0，要求观测者观测并报告、队长分配航点、三人设置自身航点、标记者提交一次标记。
5. 返回或超时后主动 abort，导出 HTML、逐秒 JSON、工具审计；核对真实调用。

默认等待上限是 900 个现实秒，max_turns=12。此处现实等待时间不计入仿真。模型调用数量受 IoA 对话及各工具任务影响，这个等待上限不等于服务端自动取消；超时后见下一节。

期望的机器检查结果：

    tool_check_passed: true
    time: 0
    status: aborted
    missing_tool_calls: []

aborted 是单时刻检查主动收尾，不代表捕获实验失败。t=0 时标记应 accepted=false；本检查不会产生“第 18 秒捕获”，动画也只有初始一帧，静止是预期。

结果保存在 simulator/outputs：

- <episode_id>.ioa-check.json：IoA 会话 ID、结论、检查结果及调用审计。
- <episode_id>.audit.json：服务实际收到并成功处理的工具操作。
- <episode_id>.json / .html：本次仿真记录和回放。

tool_check_passed 只证明所需工具调用实际发生且时钟未推进。还要在 IoA 前端或日志核对：观测者实际得到的坐标和 observed_at 经组内消息传给队长；队长指令发给各成员。不能只凭调用存在就认定协作逻辑正确，更不能据此宣称整局捕获完成。

## 6. 出错后的处理

- missing_tool_calls 非空：看缺哪个成员的哪个操作，再对照对应工具日志和 IoA 会话。模型提前结束、任务未分配、工具额度不足都可能导致缺失。
- Connection refused：先核对容器存活、端口和 agent_network，再检查日志。
- 401：身份不匹配；核对角色 env 文件是否由当前仿真 auth 文件生成。
- 403：角色越权，例如队长调用 sim_observe；不要通过放宽权限“修复”。
- 409：旧 episode/time、已结束局，或者 reset 前还有 running 局。检查脚本不自动重试写请求。
- timeout：检查报告 error；默认会中止仿真，但 HTTP 超时不会取消远端 IoA 任务。

重试前重启这三个新客户端，让各自重新创建工具容器，清除未结束的运行任务：

    docker compose -p ioa-capture -f dockerfiles/compose/capture.yaml restart

如果脚本被强制杀死而留下 running 局，重启客户端后显式清理：

    python3 -B -m simulator.ioa_check --abort-active

然后重新等待 healthy、检查工具服务，再运行 ioa_check。不要让旧任务继续运行时立即 reset 新局。

## 7. 此后怎样接逐秒调度

本节原为设计说明；逐秒三角色实现已写入 simulator/ioa_run.py，实际部署步骤以 CONTINUOUS_IOA_RUN.md 为准。下面保留设计背景。

本次先把能独立验收的接线和单时刻流程落地。下一项代码工作是新增 simulator/ioa_run.py，并同时修改 IoA 的轮次与任务生命周期；不能把 http_run 的集中转发策略称为 IoA，也不能仅在 ioa_check 外包一层循环。

连续通信调度器的实施顺序：

1. 创建一次固定三人会话，保存 comm_id，在后续时刻复用该团队。
2. 给每轮绑定 episode_id、仿真秒和轮次标识；续接会话时，把 max_turns 按本轮新增消息数计算，传播到参与成员。当前 IoA 比较的是整个历史长度，需要改为轮次基线。
3. 新增轮次状态接口，区分讨论结论、待执行任务和正在执行任务；只收到 /launch_goal 返回不等于工具全结束。
4. 连续通信下，只有本轮已关闭且工具任务全部结束，控制器才调用一次 /control/step。工具失败/超时明确记录并中止，不能当作自然逃脱。
5. 保持每个仿真秒有合法标记机会；先以每秒有界讨论完成联调，再评估并统一冻结较省调用的调度规则。模型/API 现实耗时仍不进入仿真时钟。
6. 完整三人连续通信成功后，再接九候选五人选队；最后加入消息排队和通信窗口。

加入窗口后需要独立处理消息交付与本地运动，不能要求失联成员都回话后才推进时间。上述连续通信的等待条件不能原样搬到通信窗口实验。这些调度改动应通过单时刻证据确认真实 IoA 工具链正常后再实现和验收。
