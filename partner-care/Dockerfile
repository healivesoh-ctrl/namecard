# 제휴 접수·양방향 인증 서비스 (Render/Railway/Fly 등 Docker 런타임용)
# 빌드 컨텍스트: partner-care/
FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

ENV PORT=8100 DATA_DIR=/data
EXPOSE 8100
CMD ["sh", "-c", "uvicorn app.main:app --host 0.0.0.0 --port ${PORT}"]
