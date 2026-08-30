# 친정엄마 산후도우미 신청 시스템 (Render/Railway/Fly 등 Docker 런타임용)
# 빌드 컨텍스트: care/
FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# 업로드 서류와 SQLite DB 저장 위치. Render 등에서 디스크를 붙이면 이 경로에 마운트한다.
ENV DATA_DIR=/data
ENV PORT=8000
EXPOSE 8000
CMD ["sh", "-c", "uvicorn server.app:app --host 0.0.0.0 --port ${PORT}"]
