import subprocess

import requests

STREAMER_IP = "172.20.10.5"


def get_available_streams(ip):
	resp = requests.get(f"http://{ip}:9997/v3/paths/list", timeout=5)
	resp.raise_for_status()
	return [item["name"] for item in resp.json()["items"] if item["ready"]]


def receive_stream(ip, name):
	return subprocess.Popen(
		[
			"gst-launch-1.0",
			"rtspsrc",
			f"location=rtsp://{ip}:8554/{name}",
			"protocols=udp",
			"latency=0",
			"!",
			"rtph264depay",
			"!",
			"h264parse",
			"!",
			"decodebin",
			"!",
			"videoconvert",
			"!",
			"autovideosink",
			"sync=false",
		]
	)


def main():
	streams = get_available_streams(STREAMER_IP)
	if not streams:
		print("No available streams found.")
		return

	procs = []
	for stream in streams:
		print(f"Receiving stream: {stream}")
		procs.append(receive_stream(STREAMER_IP, stream))

	try:
		for p in procs:
			p.wait()
	except KeyboardInterrupt:
		for p in procs:
			p.terminate()


if __name__ == "__main__":
	main()