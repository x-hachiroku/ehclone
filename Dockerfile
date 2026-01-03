FROM docker.io/library/python:3.14

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

WORKDIR /app

COPY pyproject.toml .
RUN pip install --index-url https://download.pytorch.org/whl/cpu torch torchvision
RUN pip install .
RUN python -c "from imagededup.methods import CNN; CNN()"

ENV PYTHONPATH="/app/src"

COPY src/ ./src/

CMD ["python", "-m", "ehclone.main"]
