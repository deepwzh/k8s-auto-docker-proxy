FROM docker.io/python:3.12-slim

RUN pip install poetry -i https://pypi.tuna.tsinghua.edu.cn/simple

WORKDIR /app
COPY pyproject.toml .
COPY poetry.lock .

RUN poetry config virtualenvs.create false && poetry install

COPY . .

ENTRYPOINT ["python", "main.py"]