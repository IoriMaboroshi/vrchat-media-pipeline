# VRChat BPlayer Proxy — Multi-platform Docker image
# Supports NVIDIA NVENC, Intel QSV, and software (libx264) encoding.

FROM python:3.11-slim AS base

# Install FFmpeg with common codecs
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install Python dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application code
COPY . .

# Create data directory
RUN mkdir -p /app/data

# Expose ports (API + Web panel)
EXPOSE 14515 8080

# Health check
HEALTHCHECK --interval=30s --timeout=5s --retries=3 \
    CMD python -c "import httpx; httpx.get('http://localhost:14515/health', timeout=5)"

CMD ["python", "main.py"]


# ---- NVIDIA GPU variant (NVENC) ----
FROM base AS nvidia

# Install NVIDIA FFmpeg with CUDA support
# Requires nvidia-container-toolkit at runtime

ENV FFMPEG_PATH=ffmpeg
ENV NVIDIA_VISIBLE_DEVICES=all
ENV NVIDIA_DRIVER_CAPABILITIES=compute,video,utility

CMD ["python", "main.py"]


# ---- Intel GPU variant (QSV) ----
FROM base AS intel

# Install Intel Media SDK
RUN apt-get update && apt-get install -y --no-install-recommends \
    intel-media-va-driver \
    vainfo \
    && rm -rf /var/lib/apt/lists/*

ENV FFMPEG_PATH=ffmpeg

CMD ["python", "main.py"]
