FROM python:3.11-slim

# Install tesseract + dependencies
RUN apt-get update && apt-get install -y \
    tesseract-ocr \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install Python deps first (faster rebuilds)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy the rest of the code
COPY . .

# If your app file is app.py and Flask app is `app`
# Ensure your Render Start Command matches this gunicorn target.
CMD ["gunicorn", "-b", "0.0.0.0:10000", "app:app"]

