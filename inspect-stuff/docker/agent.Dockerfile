FROM python:3.12-alpine

RUN apk add --no-cache bash curl ca-certificates

WORKDIR /root/notes

COPY WEBPAGES.md /root/notes/WEBPAGES.md
COPY docker/agent-entrypoint.sh /usr/local/bin/workspace-init

RUN chmod 755 /usr/local/bin/workspace-init

ENTRYPOINT ["/usr/local/bin/workspace-init"]
CMD ["sleep", "infinity"]

ENV PYTHONDONTWRITEBYTECODE=1 \
    SHELL=/bin/bash
