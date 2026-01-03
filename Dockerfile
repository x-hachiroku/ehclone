FROM docker.io/library/python:3.14-slim-trixie

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1
ENV UV_NO_CACHE=1
ENV PATH="/app/.venv/bin:$PATH"

WORKDIR /app

RUN sed -i 's/^Components: main$/Components: main non-free/' /etc/apt/sources.list.d/debian.sources \
    && apt-get update \
    && apt-get install -y --no-install-recommends 7zip 7zip-rar rar zip unzip

COPY pyproject.toml uv.lock README.md LICENSE ./
RUN apt-get install -y --no-install-recommends g++ \
    && pip install --no-cache-dir uv \
    && uv sync --frozen --no-dev --no-install-project \
    && apt-get purge -y --auto-remove g++ \
    && rm -rf /var/lib/apt/lists/*
RUN python -c "from imagededup.methods import CNN; CNN()"

COPY src/ ./src/
RUN uv sync --frozen --no-dev --no-editable \
    && python -c "import ehclone.main"

ENTRYPOINT ["python", "-m", "ehclone.main"]
