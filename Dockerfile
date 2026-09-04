FROM python:3.11-slim

# Cài đặt các gói hệ thống cần thiết cho xử lý âm thanh
RUN apt-get update && apt-get install -y --no-install-recommends \
    libsndfile1 \
    ffmpeg \
    espeak-ng \
    curl \
    git \
    && rm -rf /var/lib/apt/lists/*

# Tạo user không phải root (UID 1000) theo chuẩn Hugging Face Spaces
RUN useradd -m -u 1000 user
USER user
ENV HOME=/home/user \
    PATH=/home/user/.local/bin:$PATH \
    PYTHONUNBUFFERED=1 \
    PORT=7860

WORKDIR /home/user/app

# Cài đặt Python dependencies
COPY --chown=user requirements.txt .
RUN pip install --no-cache-dir --user -r requirements.txt

# Copy mã nguồn dự án
COPY --chown=user . .

# Port mặc định cho Hugging Face Spaces
EXPOSE 7860

# Khởi chạy bot
CMD ["python", "app.py"]
