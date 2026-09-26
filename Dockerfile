FROM python:3.11-slim

WORKDIR /app

# OCR dependencies (optional but needed for pytesseract to work)
RUN apt-get update && apt-get install -y --no-install-recommends \
    tesseract-ocr \
  && rm -rf /var/lib/apt/lists/*

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

EXPOSE 10000
ENV PORT=10000

# IMPORTANT: change "app:app" if your Flask instance isn't named app
CMD ["gunicorn", "app:app", "--bind", "0.0.0.0:10000"]

