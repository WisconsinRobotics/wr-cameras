import argparse
import subprocess
import time

import requests

POLL_INTERVAL = 2  # s between checks for new or restarted streams


def get_ready_streams(ip) -> list[str]:
	resp = requests.get(f"http://{ip}:9997/v3/paths/list", timeout=5)
	resp.raise_for_status()
	return [item["name"] for item in resp.json()["items"] if item["ready"]]


def receive_stream(ip, name) -> subprocess.Popen:
	return subprocess.Popen(
		[
			"gst-launch-1.0",
			"rtspsrc",
			f"location=rtsp://{ip}:8554/{name}",
			"protocols=udp",  # deliberate, see README "Networking: why UDP"
			# A little jitter buffer lets late/reordered packets in instead of
			# counting them as lost (latency=0 drops them).
			"latency=50",
			"udp-buffer-size=2097152",  # absorb bursts; capped by net.core.rmem_max
			"!", "rtph264depay",
			"!", "h264parse",
			"!", "decodebin",
			"!", "videoconvert",
			"!", "autovideosink", "sync=false",
		]
	)


def main():
	parser = argparse.ArgumentParser(
		prog="WR Camera Receiver",
		description="Receives and displays camera streams from the WR Camera Streamer.",
	)
	parser.add_argument("-i", "--ip", required=True, help="IP of the streamer host (e.g. 172.10.0.5)")
	args = parser.parse_args()

	# Keep polling so streams that drop out (camera lost, streamer restarted,
	# link down) reopen on their own once they're back.
	procs: dict[str, subprocess.Popen] = {}
	reachable = None
	try:
		while True:
			try:
				streams = get_ready_streams(args.ip)
			except requests.RequestException as e:
				if reachable is not False:
					print(f"Can't reach streamer at {args.ip} ({e}); retrying...")
				reachable = False
			else:
				if not reachable:
					print(f"Connected to streamer at {args.ip}.")
					if not streams:
						print("No streams yet; waiting...")
				reachable = True
				for name in streams:
					if name not in procs or procs[name].poll() is not None:
						print(f"Receiving stream: {name}")
						procs[name] = receive_stream(args.ip, name)
			time.sleep(POLL_INTERVAL)
	except KeyboardInterrupt:
		pass
	finally:
		for p in procs.values():
			if p.poll() is None:
				p.terminate()
		for p in procs.values():
			p.wait()


if __name__ == "__main__":
	main()
