FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PORT=8080

WORKDIR /app

COPY requirements.txt pyproject.toml ./
COPY boll_strategy ./boll_strategy
RUN pip install --no-cache-dir .

COPY data ./data
COPY output/dashboard.html ./output/dashboard.html
COPY server.py push_report.py ./

EXPOSE 8080
CMD ["python", "server.py"]
