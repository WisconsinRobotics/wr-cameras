.PHONY: stream

setup:
	sudo apt install libgstreamer1.0-dev libgstreamer-plugins-base1.0-dev \
		libgstreamer-plugins-bad1.0-dev gstreamer1.0-plugins-base \
		gstreamer1.0-plugins-good gstreamer1.0-plugins-bad gstreamer1.0-plugins-ugly \
		gstreamer1.0-libav gstreamer1.0-tools gstreamer1.0-x gstreamer1.0-alsa \
		gstreamer1.0-gl gstreamer1.0-gtk3 gstreamer1.0-qt5 gstreamer1.0-pulseaudio \
		gstreamer1.0-rtsp aravis-tools aravis-tools-cli gir1.2-aravis-0.8

stream: streamer.py
	uv run streamer.py

IP ?= 10.42.0.61
receive: receiver.py
	uv run receiver.py --ip $(IP)

amcrest: amcrest.py
	set -a; . ./.env; set +a; uv run amcrest.py

hotspot:
	sudo nmcli dev wifi rescan
	sudo nmcli dev wifi connect "josephs-laptop"