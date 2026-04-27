
# M3U8 XM

A WIP script that converts SiriusXM's web app into a M3U8 file.


Credits to [andrew0](https://github.com/andrew0) for the basis of this script.

## Features

- Automatic login
- Creates a full channel playlist
- Support for channel logos & genre filtering
- Xtra streams supported
- Automatically downloads a M3U file in the m3u8XM directory

Clone the project

```bash
git clone https://github.com/jhr1948/m3u8XM
```

### EDIT your config file
Don't forget to rename ``config.example.ini`` to ``config.ini`` and edit the email or username & password to your SXM account.

### config file IP & playlist_host examples
```
# Home LAN with the playlist_host IP being the computer running m3u8XM
[settings]
ip = 0.0.0.0
port = 8888
playlist_host = 192.168.x.x
playlist_scheme = http
playlist_port = 8888
playlist_output = /app/output/siriusxm.m3u
```
```
# Tailscale with playlist_host IP using the Tailscale IP address
[settings]
ip = 0.0.0.0
port = 8888
playlist_host = 100.x.x.x
playlist_scheme = http
playlist_port = 8888
playlist_output = /app/output/siriusxm.m3u
```
```
# Reverse proxy setup
[settings]
ip = 0.0.0.0
port = 8888
playlist_host = stream.yourdomain.com
playlist_scheme = https
playlist_port =
playlist_output = /app/output/siriusxm.m3u
```
## You can leave the default config lines below along
```
## You can leave lines below defaulted
xtra_queue_tracks = 6
xtra_extend_threshold = 0.70
xtra_playlist_max_age = 21600
xtra_session_ttl = 25200
```
Go to the project directory

```bash
cd m3u8XM
```

Start the server

```bash
python sxm.py
```

## Run as a Service
1. create a Service File named /etc/systemd/system/m3u8XM.service (edit your actual paths & username)
```
[Unit]
Description=M3UXM Service
After=network.target

[Service]
ExecStart=/usr/bin/python3 /home/path/to/m3u8XM/sxm.py
WorkingDirectory=/home/path/to/m3u8XM
StandardOutput=inherit
StandardError=inherit
Restart=always
User=yourusername

[Install]
WantedBy=multi-user.target
```
2. Enable and Start
```
sudo systemctl daemon-reload      # Reload systemd to recognize new service
sudo systemctl enable m3u8XM      # Set to start on boot
sudo systemctl start m3u8XM       # Start it now
```

## Docker Compose setup
Create a working directory:
```
mkdir -p m3u8xm/output
cd m3u8xm
```
Your folder should contain:
```
m3u8xm/
├── sxm.py
├── config.ini
├── Dockerfile
├── requirements.txt
└── output/
```
requirements.txt
```
requests
```
Dockerfile
```
FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY sxm.py .

EXPOSE 8888

CMD ["python", "-u", "sxm.py"]
```
config.ini (example) (make sure to use above ini exeamples for reverse proxy)
```
[account]
email = your_email@example.com
username = example
password = your_password

[settings]
ip = 0.0.0.0
port = 8888
playlist_host = 192.168.x.x
playlist_scheme = http
playlist_port = 8888
playlist_output = /app/output/siriusxm.m3u
```
Build the Docker Image (one-time)
```
cd /mnt/user/appdata/m3u8xm
docker build --no-cache -t m3u8xm:latest .
```
docker-compose.yml
```
services:
  m3u8xm:
    image: m3u8xm:latest
    container_name: m3u8xm
    restart: unless-stopped
    ports:
      - "8888:8888"
    volumes:
      - ./config.ini:/app/config.ini:ro
      - ./output:/app/output
```
Start the Container
```
docker compose up -d
```

## License

[MIT](https://choosealicense.com/licenses/mit/)

This project is not affiliated with SiriusXM
