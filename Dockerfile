FROM python:3.12-slim
ENV PYTHONUNBUFFERED=1 CONFIG_DIR=/config SHOWS_DIR=/shows PORT=8099
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg && rm -rf /var/lib/apt/lists/*
RUN pip install --no-cache-dir hue-entertainment==0.1.2 aiohttp numpy scipy
WORKDIR /app
COPY app/ /app/
VOLUME ["/config", "/shows"]
EXPOSE 8099
CMD ["python", "main.py"]
