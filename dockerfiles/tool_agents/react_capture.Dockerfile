# Dedicated ReAct backend for the three-role IoA capture simulation.
# The original react.Dockerfile and its unrelated tool dependencies remain intact.
FROM python:3.10.13-slim

WORKDIR /app
EXPOSE 7070

COPY im_client/agents/react/requirements_capture.txt /app/requirements.txt
RUN pip config set global.index-url https://pypi.tuna.tsinghua.edu.cn/simple \
    && pip install --no-cache-dir -r /app/requirements.txt

COPY im_client/agents/react /app
COPY im_client/agents/tools /app/tools
COPY common /app/common
COPY im_client/llms /app/llms
COPY im_client/memory /app/memory
COPY im_client/prompts /app/prompts

# Only agent-facing HTTP adapters; no simulator core, routes or controller token.
COPY simulator/__init__.py simulator/http_client.py simulator/ioa_tools.py simulator/experiment_rules.py /app/simulator/
RUN python -m simulator.ioa_tools --output-dir /app/simulator/tool_configs

# Fail during build if a missing dependency still blocks capture-mode imports.
# Placeholder values are used only by this RUN layer; no model request is sent.
RUN SIM_URL=http://ioa-simulator:8090 \
    SIM_AGENT_TOKEN=build-check-only \
    OPENAI_API_KEY=build-check-only \
    AGENT_MODEL=Qwen3.8-Flash \
    TOOLS_CONFIG=/app/simulator/tool_configs/observer.json \
    python -c "import react_agent; assert 'sim_observe' in react_agent.TOOL_MAPPING; assert not react_agent.BROWSER_TOOLS_MAPPING"

ENTRYPOINT ["python", "react_agent.py"]
