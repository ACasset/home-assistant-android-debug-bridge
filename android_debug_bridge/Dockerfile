FROM python:3.13-alpine

RUN apk add --no-cache libusb
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app.py index.html ./
CMD ["python3", "-u", "/app/app.py"]
