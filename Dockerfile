FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PORT=8080

WORKDIR /app

COPY requirements.txt pyproject.toml ./
COPY boll_strategy ./boll_strategy
COPY etf_momentum ./etf_momentum
RUN pip install --no-cache-dir .

COPY data ./data
COPY output/dashboard.html ./output/dashboard.html
COPY _etf_momentum_template.html ./_etf_momentum_template.html
COPY server.py push_report.py ./

EXPOSE 8080
CMD ["python", "server.py"]
