FROM python:3.13-slim
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 TOKEN_FILE=/data/tokens.json
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt \
    && useradd --uid 10001 --create-home exporter \
    && mkdir /data && chown exporter:exporter /data
COPY exporter.py .
USER exporter
EXPOSE 9809
ENTRYPOINT ["python", "exporter.py"]
CMD ["serve"]
