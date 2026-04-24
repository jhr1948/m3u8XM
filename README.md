
# M3U8 XM

A WIP script that converts SiriusXM's web app into a M3U8 file.


Credits to [andrew0](https://github.com/andrew0) for the basis of this script.

## Features

- Automatic login
- Creates a full channel playlist
- Support for channel logos & genre filtering
- Xtra streams supported
- Automatically downloads a M3U file in the m3u8XM directory

## Run Locally

Clone the project

```bash
git clone https://github.com/jhr1948/m3u8XM
```

Go to the project directory

```bash
cd m3u8XM
```

### Add your config file
Don't forget to rename ``config.example.ini`` to ``config.ini`` and edit the email or username & password to your SXM account.

### config file IP & playlist_host examples
```
# Home LAN with the playlist_host IP being the computer running m3u8XM
ip = 0.0.0.0
playlist_host = 192.168.x.x
```
```
# Tailscale with playlist_host IP using the Tailscale IP address
ip = 0.0.0.0
playlist_host = 100.x.x.x
```
```
# Reverse proxy setup
ip = 127.0.0.1
playlist_host = stream.yourdomain.com
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

## License

[MIT](https://choosealicense.com/licenses/mit/)

This project is not affiliated with SiriusXM
