# Lean web container: search UI, player, streaming, lyrics, live grading.
# No ML deps (torch/demucs/basic-pitch/librosa) - those belong to the
# separate pipeline container (docker/pipeline.Dockerfile) so this one stays
# small and fast to rebuild. See CLAUDE.md's "Docker deployment" section for
# why the split is two containers sharing a SQLite DB + data volume rather
# than a network API between them.

FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    APP_HOST=0.0.0.0 \
    APP_PORT=5000

WORKDIR /app

COPY requirements.txt /app/
RUN pip install --no-cache-dir --upgrade pip \
    && pip install --no-cache-dir -r requirements.txt

COPY . /app
# The pipeline image owns requirements-ml.txt's install; this image never
# imports vocal_transcribe/tempo's heavy deps (they're lazy-imported inside
# their own functions, which this container's code path never calls).

EXPOSE 5000

# debug=False (see app.py's FLASK_DEBUG check): no reloader, and critically,
# no queue worker - see the docstring on start_library_worker() and
# worker.py. threaded=True is set unconditionally in app.py itself.
ENV FLASK_DEBUG=false
CMD ["python", "app.py"]
