# PyTorch 2.7.1 + cu128 — required for RTX 5070 Ti (Blackwell, sm_120).
# Default torch wheels (cu121/cu124) only include up to sm_90 (Hopper)
# and crash with "no kernel image is available for execution on the device".
FROM pytorch/pytorch:2.7.1-cuda12.8-cudnn9-runtime

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY main.py .

EXPOSE 8000
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
