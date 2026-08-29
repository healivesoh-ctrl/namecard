# SNS 자동화 웹 서비스 (Render/Railway/Fly 등 Docker 런타임용)
# 빌드 컨텍스트: social-automation/
FROM python:3.12-slim

# 카드 이미지의 한글·중문 렌더링용 CJK 폰트
RUN apt-get update \
    && apt-get install -y --no-install-recommends fonts-noto-cjk \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY server/requirements.txt server/requirements.txt
RUN pip install --no-cache-dir -r server/requirements.txt

COPY . .

ENV PORT=8000
EXPOSE 8000
CMD ["sh", "-c", "uvicorn server.app:app --host 0.0.0.0 --port ${PORT}"]
