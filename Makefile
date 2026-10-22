.PHONY: stream

stream: streamer.py
	uv run streamer.py

IP ?= 172.20.10.5
receive: receiver.py
	uv run receiver.py --ip $(IP)

amcrest: amcrest.py
	set -a; . ./.env; set +a; uv run amcrest.py
