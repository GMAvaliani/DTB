FROM python:3.11-slim

WORKDIR /app
COPY pyproject.toml README.md ./
COPY src ./src
COPY frontend ./frontend
RUN pip install --no-cache-dir .

ENV DTB_DATA_DIR=/data
VOLUME ["/data"]
EXPOSE 8000

CMD ["dtb", "serve", "--host", "0.0.0.0", "--port", "8000"]
