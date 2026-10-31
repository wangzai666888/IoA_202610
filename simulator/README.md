# 最小协同捕获仿真与动画

HTTP 服务和时钟设计请见 [HTTP 与 IoA 实施指南](HTTP_IOA_GUIDE.md)。三角色工具接线、配置、Compose 和单时刻联调请见 [Ubuntu 接线操作单](UBUNTU_IOA_WIRING.md)；三角色逐秒协同请见 [逐秒 IoA 调度操作单](CONTINUOUS_IOA_RUN.md)。九候选五人固定选队及连续通信部署见 [五人实施操作单](FIVE_TEAM_IOA_GUIDE.md)。http_run 不调用模型，ioa_check 与 ioa_run 会调用模型。通信窗口尚未实现。

`python3 -B -m simulator.ioa_run --max-rounds 0` 完成后，除轨迹 JSON、工具审计、HTML 回放与 `.ioa-run.json` 外，还会保存该局的 `.ioa-chat.json` 和 `.ioa-chat.txt`。两份对话文件来自 IoA Server 按 comm_id 保存的组内消息；导出状态与文件名写在 `.ioa-run.json` 中。

在 IoA 工程根目录运行：

    python -B -m simulator.scripted_run

命令会打印捕获结果，并默认在 simulator/outputs/ 中生成同名的 .html 动画回放和 .json 逐秒记录。用浏览器打开 HTML 即可播放、暂停、拖动时间轴或改变速度。页面可显示截至当前时刻的轨迹、距离圈和关键事件；“预设目标路线”只供实验结束后的评估回放，默认隐藏。HTML 内嵌数据，不依赖网络或额外绘图库。

可用 --replay-output /path/to/run.html 指定输出位置，或用 --no-replay 只打印结果。运行以下命令检查仿真规则和回放导出：

    python -B -m unittest discover -s simulator/tests -v

修改回放页面后，先重新运行仿真生成 HTML，再运行下面的 JavaScript 运行时回归检查（需要 Node.js，仅用于开发验证）。检查会模拟浏览器的全局变量约束以及 DOM、Canvas 和计时器，验证初始化、播放、暂停、拖动、逐帧前后移动、变速、结束和重新播放；它不替代真实浏览器的画面验收。

    node simulator/tests/test_replay.cjs

以后接入 IoA 后，可复用 simulator/replay.py 中的 write_replay(env, html_path)：一局结束时传入同一个仿真环境对象，即可把真实逐秒轨迹和动作事件导出为回放。正式实验应为每局使用唯一输出文件名，并另存场景、通信条件、受限成员和模型配置。回放记录含目标真值及预设路线，只应交给评估与展示侧，不应在运行期间提供给 Agent。

旧集中式海上仿真工程没有现成的动画导出代码。这里仅沿用“每步更新环境状态”的思路，回放完全基于本仿真核心已经保存的 trace，不依赖旧工程的集中式观测或评测结构。
