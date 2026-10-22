.PHONY: stream

stream: streamer.py
	uv run streamer.py

receive: receiver.py
	uv run receiver.py --ip $(IP)

amcrest: amcrest.py
	set -a; . ./.env; set +a; uv run amcrest.py