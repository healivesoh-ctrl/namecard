# 카카오 알림톡 자동 발송 웹 서비스 (Render/Railway/Fly 등 Docker 런타임용)
# 빌드 컨텍스트: kakao-alimtalk/
FROM python:3.12-slim

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .

# SQLite 저장 위치. 재배포 후에도 예약을 유지하려면 이 경로에 영구 디스크를 마운트한다.
ENV DB_PATH=/app/data/alimtalk.db
ENV PORT=8000
EXPOSE 8000
CMD ["sh", "-c", "uvicorn server.app:app --host 0.0.0.0 --port ${PORT}"]
