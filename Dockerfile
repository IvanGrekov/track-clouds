FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install --no-cache-dir .

RUN useradd --create-home --uid 10001 monitor \
    && mkdir -p /app/.state \
    && chown monitor:monitor /app/.state
USER monitor

CMD ["telegram-monitor", "run"]
