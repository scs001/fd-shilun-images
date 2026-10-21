# fd--api 服务端镜像 — FastAPI + uvicorn。
# 发布通道：GitHub Actions -> TCR（.github/workflows/image.yml，配方 docs/IMAGE-RELEASE.md）。
# 硬性要求（IMAGE-RELEASE 六步清单）：上游源为默认；国内镜像源一律 ARG 且默认空。
FROM python:3.12-slim

ARG PIP_INDEX_URL=""

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

COPY pyproject.toml README.md ./
COPY fd_shilun_api ./fd_shilun_api
COPY data ./data

RUN if [ -n "$PIP_INDEX_URL" ]; then pip install --no-cache-dir -i "$PIP_INDEX_URL" .; else pip install --no-cache-dir .; fi \
 && pip install --no-cache-dir https://github.com/scs001/fd-open-data-protocol-images/archive/refs/heads/main.tar.gz \
 && useradd -m -u 1000 appuser

USER appuser

EXPOSE 8000

CMD ["uvicorn", "fd_shilun_api.main:app", "--host", "0.0.0.0", "--port", "8000"]
