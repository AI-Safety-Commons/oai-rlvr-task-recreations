FROM alpine:3.22

RUN apk add --no-cache squid
COPY docker/squid.conf /etc/squid/squid.conf
USER squid
CMD ["squid", "-N", "-f", "/etc/squid/squid.conf"]
