FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

WORKDIR /app

COPY pyproject.toml .
RUN pip install .

ENV PYTHONPATH="/app/src"

COPY src/ ./src/

CMD ["python", "-m", "ehclone.main"]
