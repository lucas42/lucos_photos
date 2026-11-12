FROM python:3.15-slim

WORKDIR /app

# Install OS-level dependencies needed by OpenCV, InsightFace, and ffmpeg/ffprobe.
# gcc and g++ are required to build insightface and stringzilla from source.
# ffmpeg provides both the ffmpeg and ffprobe binaries used by the video pipeline.
RUN apt-get update && apt-get install -y --no-install-recommends \
    libglib2.0-0 \
    libgl1 \
    libgomp1 \
    gcc \
    g++ \
    ffmpeg \
    && rm -rf /var/lib/apt/lists/*

# Install heavy ML dependencies before application code so this layer is
# cached across code-only changes. These are pinned to exact versions so
# the layer only rebuilds when versions are explicitly bumped.
COPY worker/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Download and bake InsightFace buffalo_l model files into the image.
# This avoids slow cold-start downloads and external download failures in production.
# The download is verified by insightface's own checksum mechanism; a failed download
# will cause the build to fail loudly here rather than silently produce a broken image.
ENV INSIGHTFACE_ROOT=/opt/insightface
RUN python -c "from insightface.app import FaceAnalysis; \
    app = FaceAnalysis(name='buffalo_l', root='/opt/insightface', allowed_modules=['detection', 'recognition']); \
    app.prepare(ctx_id=-1, det_size=(640, 640)); \
    print('InsightFace buffalo_l model downloaded and verified successfully')"

# Shared package after the heavy layers, so changes to it don't invalidate them.
COPY shared/ /shared/
RUN pip install --no-cache-dir /shared/

# VERSION after the last RUN, so a version bump doesn't invalidate any layer above.
ARG VERSION
ENV VERSION=$VERSION

# Copy application code last so code changes don't invalidate the ML layer cache
COPY worker/app/ ./app/

CMD ["python", "-m", "app.main"]
