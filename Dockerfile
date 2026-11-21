# Same base image (by digest) that the organizers labeled the commits in.
FROM python:3.13-slim@sha256:e48f59cdba69a3cbd9ed2cf0f348d90d45a8d350d98134cf1070bc106ee9ed90

# Exact package set of the labeling environment (PyPI only).
COPY requirements.lock /tmp/requirements.lock
RUN pip install --no-cache-dir -r /tmp/requirements.lock

# sqlglot at the pinned commit, precompiled so imports are cheap at run time.
COPY tools/fetch_sqlglot.py /tmp/fetch_sqlglot.py
RUN python /tmp/fetch_sqlglot.py /opt/sqlglot \
 && python -m compileall -q /opt/sqlglot/sqlglot /opt/sqlglot/tests \
 && chmod -R a+rX /opt/sqlglot

COPY br /app/br
COPY maps /app/maps
RUN python -m compileall -q /app/br && chmod -R a+rX /app

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=/app SKIP_INTEGRATION=0 BR_SQLGLOT=/opt/sqlglot BR_MAPS=/app/maps
CMD ["python", "-m", "br.main"]
