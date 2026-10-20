import atexit
import os
import signal
import subprocess
import sys
import time

import docker
import requests


class MediaMTXContainer:
	def __init__(self):
		self.client = docker.from_env()
		self.container = None
		self.ip = None

	def start_container(self):
		self.container = self.client.containers.run(
			"bluenviron/mediamtx",
			detach=True,
			auto_remove=True,  # no stale containers after stop
			environment={"MTX_API": "yes"},
			# Published ports are for remote receivers (DNAT keeps UDP source ports).
			# Local publishing bypasses them by talking to the container IP directly.
			ports={
				"8554/tcp": 8554,
				"9997/tcp": 9997,
				"8000/udp": 8000,
				"8001/udp": 8001,
			},
			volumes={
				os.path.abspath("mediamtx.yml"):
					{"bind": "/mediamtx.yml", "mode": "ro"}
			},
		)
		self.container.reload()
		self.ip = self._get_ip()
		self._wait_until_ready()
		print(f"Container started. MediaMTX reachable locally at {self.ip}")

	def _get_ip(self):
		settings = self.container.attrs["NetworkSettings"]
		ip = settings.get("IPAddress")
		if ip:
			return ip
		# Custom networks / compose: IP lives under Networks[<name>]
		for net in settings.get("Networks", {}).values():
			if net.get("IPAddress"):
				return net["IPAddress"]
		raise RuntimeError("Could not determine the container's IP address.")

	def _wait_until_ready(self, timeout=15):
		deadline = time.time() + timeout
		while time.time() < deadline:
			try:
				requests.get(f"http://{self.ip}:9997/v3/paths/list", timeout=1)
				return
			except requests.RequestException:
				time.sleep(0.5)
		raise RuntimeError("MediaMTX did not become ready in time.")

	def stop_container(self):
		if not self.container:
			return
		try:
			self.container.stop()
			print("Container stopped.")
		except docker.errors.NotFound:
			pass  # already stopped and auto-removed

		self.container = None

	def handle_exit(self, signum=None, frame=None):
		print("Signal received. Stopping container...")
		self.stop_container()
		exit(0)


CAMERA_SCAN_START = 0
CAMERA_SCAN_STOP = 8
CAMERA_SCAN_STEP = 2

def find_available_cameras() -> list[int]:
	available_cameras = []
	for cam_id in range(CAMERA_SCAN_START, CAMERA_SCAN_STOP, CAMERA_SCAN_STEP):
		device_path = f"/dev/video{cam_id}"
		if os.path.exists(device_path):
			available_cameras.append(cam_id)
	return available_cameras

camera_processes = []

def launch_camera(video_id, host):
	# Publish to the container's bridge IP (NOT 127.0.0.1) so traffic bypasses
	# docker-proxy, which rewrites the UDP source port and breaks MediaMTX's check.
	proc = subprocess.Popen(
		[
			"gst-launch-1.0",
			"v4l2src",
			f"device=/dev/video{video_id}",
			"!",
			"image/jpeg, width=1280, height=720, framerate=30/1",
			"!",
			"jpegdec",
			"!",
			"videoconvert",
			"!",
			"x264enc",
			"tune=zerolatency",
			"bitrate=1500",
			"speed-preset=ultrafast",
			"key-int-max=30",
			"!",
			"h264parse",
			"config-interval=-1",  # resend SPS/PPS so late joiners can decode
			"!",
			"rtspclientsink",
			"protocols=udp",
			f"location=rtsp://{host}:8554/stream{video_id}",
		]
	)
	camera_processes.append(proc)
	print(f"Camera stream launched for video ID {video_id}.")


def stop_cameras():
	for proc in camera_processes:
		if proc.poll() is None:
			proc.terminate()
	for proc in camera_processes:
		try:
			proc.wait(timeout=5)
		except subprocess.TimeoutExpired:
			proc.kill()
	camera_processes.clear()


def main():
	media_mtx = MediaMTXContainer()

	def cleanup():
		stop_cameras()
		media_mtx.stop_container()

	atexit.register(cleanup)
	# Convert signals into a normal exit so atexit runs cleanup exactly once.
	signal.signal(signal.SIGINT, lambda *_: sys.exit(0))
	signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))

	media_mtx.start_container()

	camera_ids = find_available_cameras()

	if not camera_ids:
		print("No available cameras found.")
		return

	for video_id in camera_ids:
		launch_camera(video_id=video_id, host=media_mtx.ip)

	while True:
		if any(p.poll() is not None for p in camera_processes):
			print("A camera pipeline exited. Shutting down.")
			sys.exit(1)
		time.sleep(1)


if __name__ == "__main__":
	main()