# The scorer builds this on linux/amd64 and runs it with no network.
# Same base image digest the organisers labelled every commit in.
FROM python:3.13-slim@sha256:e48f59cdba69a3cbd9ed2cf0f348d90d45a8d350d98134cf1070bc106ee9ed90

# sqlglot source at the pinned commit: the selector reads the pre-change files to
# apply each patch and compare syntax trees. Fetched from sqlglot's own repository.
ARG SQLGLOT_SHA=eb11aec4be8cb2122afe713a4816662c3f0fb2fa
RUN python - <<EOF
import io, os, tarfile, urllib.request
sha = "${SQLGLOT_SHA}"
url = f"https://github.com/tobymao/sqlglot/archive/{sha}.tar.gz"
data = urllib.request.urlopen(url, timeout=300).read()
with tarfile.open(fileobj=io.BytesIO(data)) as tar:
    tar.extractall("/opt", filter="data")
os.rename(f"/opt/sqlglot-{sha}", "/opt/sqlglot")
EOF

COPY selector.py /app/selector.py
COPY br /app/br
COPY maps /app/maps
RUN python -m compileall -q /app && chmod -R a+rX /app /opt/sqlglot

ENV BR_SQLGLOT=/opt/sqlglot
CMD ["python", "/app/selector.py"]
