FROM python:3.11-slim

ENV DEBIAN_FRONTEND=noninteractive

RUN apt-get update && apt-get install -y --no-install-recommends \
        procps \
        curl wget pv jq aria2 \
        ffmpeg \
        locales \
        git unzip \
        mediainfo \
        libcurl4-openssl-dev \
        libjpeg62-turbo \
        libmagic1 \
        file \
    && rm -rf /var/lib/apt/lists/*

RUN curl -fsSL https://rclone.org/install.sh | bash

RUN ln -snf /usr/share/zoneinfo/Asia/Yangon /etc/localtime && \
    echo "Asia/Yangon" > /etc/timezone

WORKDIR /usr/src/app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

CMD ["bash", "start.sh"]