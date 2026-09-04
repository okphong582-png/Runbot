FROM python:3.11-slim

# Cài đặt các gói hệ thống cần thiết cho xử lý âm thanh
RUN apt-get update && apt-get install -y --no-install-recommends \
    libsndfile1 \
    ffmpeg \
    espeak-ng \
    curl \
    git \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Cài đặt Python dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy mã nguồn dự án
COPY . .

# Port mặc định cho Hugging Face Spaces / Web Health Check
EXPOSE 7860

ENV PYTHONUNBUFFERED=1
ENV PORT=7860

# Khởi chạy bot
CMD ["python", "app.py"]
