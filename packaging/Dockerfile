FROM debian:13.7-slim@sha256:a72b4433de1cf7d6bae0c4cee74d0547f58601a643b3375f070c51600d63b6b2

RUN printf '%s\n' \
    'deb [check-valid-until=no] http://snapshot.debian.org/archive/debian/20261110T000000Z trixie main' \
    'deb [check-valid-until=no] http://snapshot.debian.org/archive/debian-security/20261110T000000Z trixie-security main' \
    > /etc/apt/sources.list \
    && rm /etc/apt/sources.list.d/debian.sources

RUN apt-get update && apt-get install -y --no-install-recommends ca-certificates \
    && sed -i 's|http://|https://|g' /etc/apt/sources.list

RUN apt-get update && apt-get install -y --no-install-recommends \
    dpkg-dev debhelper dh-python python3 pandoc lintian man-db git sudo zstd \
    && rm -rf /var/lib/apt/lists/*

RUN apt-get update && apt-get install -y --no-install-recommends build-essential \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /tmp
