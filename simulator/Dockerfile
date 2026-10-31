FROM python:3.10.13-slim
WORKDIR /app
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
COPY *.py /app/simulator/
COPY replay_template.html /app/simulator/
COPY scenarios /app/simulator/scenarios
EXPOSE 8090
CMD ["python", "-m", "simulator.service", "--host", "0.0.0.0", "--auth-file", "/run/simulator/auth.json", "--output-dir", "/app/outputs"]
