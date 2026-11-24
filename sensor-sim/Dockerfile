FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY simulator ./simulator
COPY simctl /usr/local/bin/simctl
# Windows · Dropbox 에서 복사하면 실행 권한이 사라지거나 줄 끝이 CRLF 가 되어 simctl 이 실행되지 않는다 → 여기서 바로잡음
RUN sed -i 's/\r$//' /usr/local/bin/simctl && chmod +x /usr/local/bin/simctl
COPY sim.example.yaml /config/sim.yaml
ENV PYTHONUNBUFFERED=1
ENTRYPOINT ["python", "-m", "simulator"]
CMD ["--config", "/config/sim.yaml"]
