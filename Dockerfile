FROM python:3.12-slim
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 DATA_DIR=/tmp/ledgerflow
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app.py ./
COPY ledgerflow ./ledgerflow
COPY workflows ./workflows
COPY data/customers.yaml data/policies.yaml ./data/
COPY frontend ./frontend
RUN useradd -m -u 1000 app && chown -R app /app
USER app
EXPOSE 8080
CMD ["python", "app.py"]
