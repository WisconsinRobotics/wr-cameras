import atexit
import functools
import os
import re
import shutil
import signal
import subprocess
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import docker
import requests

MEDIAMTX_CONFIG = Path(__file__).with_name("mediamtx.yml")

# Supervision: cameras can drop out at any time, so the streamer keeps
# rescanning and restarts pipelines instead of exiting.
RESCAN_INTERVAL = 5  # s between camera scans (also the retry delay)
STARTUP_GRACE = 30  # s a new pipeline gets to start sending (Triton setup is slow)
STALL_TIMEOUT = 10  # s without new data before a running pipeline counts as hung

# USB cameras: each one exposes two /dev/video nodes (capture + metadata), so
# only every other node is a usable stream.
V4L2_SCAN = range(0, 8, 2)

# GigE Vision Tritons (TRT016S)
ARV_TOOL = "arv-tool-0.8"
TRITON_VENDOR = "Lucid Vision Labs"
TRITON_SENSOR_WIDTH = 1440
TRITON_SENSOR_HEIGHT = 1080
# 2x2 binning on the camera: 720x540 of the full view, 4x less data to move
# over GigE and debayer than the full 1440x1080.
TRITON_BINNING = 2
TRITON_WIDTH = 640
TRITON_HEIGHT = 480
TRITON_FPS = 30


class MediaMTXContainer:
	def __init__(self):
		self.client = docker.from_env()
		self.container = None
		self.ip = None

	def start(self):
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
			volumes={str(MEDIAMTX_CONFIG): {"bind": "/mediamtx.yml", "mode": "ro"}},
		)
		self.container.reload()
		self.ip = self._get_ip()
		self._wait_until_ready()
		print(f"MediaMTX started, reachable locally at {self.ip}")

	def _get_ip(self):
		settings = self.container.attrs["NetworkSettings"]
		if settings.get("IPAddress"):
			return settings["IPAddress"]
		# Custom networks / compose: IP lives under Networks[<name>]
		for net in settings.get("Networks", {}).values():
			if net.get("IPAddress"):
				return net["IPAddress"]
		raise RuntimeError("Could not determine the container's IP address.")

	def _list_paths(self, timeout):
		resp = requests.get(f"http://{self.ip}:9997/v3/paths/list", timeout=timeout)
		resp.raise_for_status()
		return resp.json()["items"]

	def _wait_until_ready(self, timeout=15):
		deadline = time.time() + timeout
		while time.time() < deadline:
			try:
				self._list_paths(timeout=1)
				return
			except requests.RequestException:
				time.sleep(0.5)
		raise RuntimeError("MediaMTX did not become ready in time.")

	def inbound_bytes(self) -> dict[str, int] | None:
		"""Bytes received so far per stream, or None if the API is unreachable."""
		try:
			items = self._list_paths(timeout=2)
		except requests.RequestException:
			return None
		# Newer MediaMTX renamed bytesReceived to inboundBytes.
		return {i["name"]: i.get("inboundBytes", i.get("bytesReceived", 0)) for i in items}

	def stop(self):
		if not self.container:
			return
		try:
			self.container.stop()
			print("MediaMTX stopped.")
		except docker.errors.NotFound:
			pass  # already stopped and auto-removed
		self.container = None


# ---------------------------------------------------------------------------
# Camera discovery
# ---------------------------------------------------------------------------


def find_v4l2_cameras() -> list[int]:
	return [i for i in V4L2_SCAN if os.path.exists(f"/dev/video{i}")]


@functools.cache
def have_arv_tool() -> bool:
	if shutil.which(ARV_TOOL):
		return True
	print(f"{ARV_TOOL} not found (sudo apt install aravis-tools-cli); skipping Tritons.")
	return False


def find_triton_cameras() -> list[str]:
	"""Return Aravis device IDs (e.g. 'Lucid Vision Labs-TRT016S-C-264101512') of
	GigE Vision Triton cameras visible on the network."""
	if not have_arv_tool():
		return []
	try:
		out = subprocess.run(
			[ARV_TOOL], capture_output=True, text=True, timeout=10, check=False
		).stdout
	except subprocess.TimeoutExpired:
		print(f"{ARV_TOOL} timed out; skipping Tritons this scan.")
		return []
	# Each line looks like: "<vendor>-<model>-<serial> (<ip>)"
	return [
		line.rsplit(" (", 1)[0]
		for line in out.splitlines()
		if line.startswith(TRITON_VENDOR)
	]


def arv_control(camera_id, *features) -> subprocess.CompletedProcess | None:
	"""Read ("Name") or write ("Name=value") camera features in order.
	Returns None if arv-tool timed out."""
	try:
		return subprocess.run(
			[ARV_TOOL, "-n", camera_id, "control", *features],
			capture_output=True,
			text=True,
			timeout=15,
			check=False,
		)
	except subprocess.TimeoutExpired:
		return None


def reset_triton(camera_id):
	"""Load the factory-default user set. The camera keeps settings from earlier
	sessions (e.g. another machine) until power-cycled, and stale ones can make
	aravissrc's setup writes time out."""
	result = arv_control(camera_id, "UserSetSelector=Default", "UserSetLoad")
	if result is None:
		print(f"Warning: resetting {camera_id} timed out.")
	elif result.returncode != 0:
		print(f"Warning: could not reset {camera_id}: {result.stderr.strip()}")


def bin_triton(camera_id) -> int:
	"""Turn on binning so the camera sends a smaller image of the full field of
	view. Returns the binning factor in effect (1 if the camera refused).

	Color Tritons only bin on the camera's FPGA ("Digital"), and only in raw
	Bayer formats. aravissrc can't select that mode itself (its `features`
	are applied after its binning write), so it's done here beforehand."""
	arv_control(
		camera_id,
		"BinningSelector=Digital",
		f"BinningHorizontal={TRITON_BINNING}",
		f"BinningVertical={TRITON_BINNING}",
	)
	# Read back rather than trust the exit code: arv-tool doesn't reliably
	# fail on a missing feature or a rejected value.
	result = arv_control(camera_id, "BinningHorizontal", "BinningVertical")
	values = re.findall(r"Binning(?:Horizontal|Vertical) = (\d+)", result.stdout) if result else []
	if values == [str(TRITON_BINNING)] * 2:
		return TRITON_BINNING
	print(f"Warning: couldn't enable binning on {camera_id}; using full resolution.")
	return 1


def prepare_triton(camera_id) -> list[str]:
	reset_triton(camera_id)
	return triton_source(camera_id, bin_triton(camera_id))


def discover_cameras() -> dict[str, Callable[[], list[str]]]:
	"""Map each connected camera's stream name to a function that readies the
	camera and returns its GStreamer source."""
	cameras = {f"stream{i}": functools.partial(v4l2_source, i) for i in find_v4l2_cameras()}
	for camera_id in find_triton_cameras():
		serial = camera_id.rsplit("-", 1)[-1]
		cameras[f"triton{serial}"] = functools.partial(prepare_triton, camera_id)
	return cameras


# ---------------------------------------------------------------------------
# GStreamer pipelines. Each source ends in raw video that x264enc accepts.
# ---------------------------------------------------------------------------


def v4l2_source(video_id) -> list[str]:
	return [
		"v4l2src", f"device=/dev/video{video_id}",
		"!", "image/jpeg, width=1280, height=720, framerate=30/1",
		"!", "jpegdec",
		"!", "videoconvert",
	]


def triton_source(camera_id, binning=1) -> list[str]:
	# Grab the whole sensor (binned on the camera if possible) as raw Bayer
	# (1 byte/px, lightest on the GigE link), debayer on the host, then
	# downscale so the whole field of view is kept.
	return [
		"aravissrc", f"camera-name={camera_id}", "exposure-auto=on", "gain-auto=on",
		# Pin the full (binned) sensor size and frame rate. Left open, caps
		# negotiation can fixate on a tiny ROI at the camera's max rate (seen:
		# 354 Hz), which the camera then chokes on during setup.
		"!", (
			f"video/x-bayer, format=rggb, width={TRITON_SENSOR_WIDTH // binning}, "
			f"height={TRITON_SENSOR_HEIGHT // binning}, framerate={TRITON_FPS}/1"
		),
		# Queues split the pipeline into threads; without them every element runs
		# on one core and the Pi can fall behind.
		# leaky=downstream drops old frames rather than building up lag.
		"!", "queue", "max-size-buffers=2", "leaky=downstream",
		"!", "bayer2rgb",
		"!", "queue", "max-size-buffers=2",
		# Scale before converting so videoconvert works on 640x480, not 1440x1080.
		"!", "videoscale", "n-threads=4",
		"!", f"video/x-raw, width={TRITON_WIDTH}, height={TRITON_HEIGHT}",
		"!", "videoconvert", "n-threads=4",
		"!", "video/x-raw, format=I420",
	]


# H.264 encoders, both tuned for lossy UDP links (see README "Networking: why UDP").
X264_ENCODER = [
	"x264enc",
	"tune=zerolatency",
	"bitrate=1500",
	"speed-preset=ultrafast",
	# Rolling intra refresh instead of whole keyframes: no big bursts that
	# overflow a thin link, and a lost packet's damage heals within
	# key-int-max frames rather than lasting until the next keyframe.
	"intra-refresh=true",
	"key-int-max=15",
	"vbv-buf-capacity=200",  # cap bitrate spikes to ~200 ms of data
]

# Rockchip hardware encoder (Orange Pi 5 Plus / RK3588), from the
# gstreamer-rockchip plugin. It has no intra refresh, so it falls back to
# periodic keyframes: same 0.5 s heal time, but burstier on a thin link.
MPP_ENCODER = [
	"mpph264enc",
	"rc-mode=cbr",
	"bps=1500000",
	"gop=15",  # keyframe every 15 frames, like x264's key-int-max
	"header-mode=each-idr",  # SPS/PPS with every keyframe
	# Default lets 16 frames (~0.5 s) pile up inside the encoder when it falls
	# behind; 1 keeps it real-time (the leaky queue in front drops instead).
	"max-pending=1",
]


def encoder_works(encoder) -> bool:
	"""Encode a few test frames, catching a missing plugin, unsupported
	properties, or no access to the hardware (/dev/mpp_service). Prints
	GStreamer's error on failure so the cause is visible."""
	try:
		result = subprocess.run(
			[
				"gst-launch-1.0", "videotestsrc", "num-buffers=5",
				"!", "video/x-raw, format=I420, width=640, height=480, framerate=30/1",
				"!", *encoder, "!", "h264parse", "!", "fakesink",
			],
			capture_output=True,
			text=True,
			timeout=10,
			check=False,
		)
	except subprocess.TimeoutExpired:
		print(f"{encoder[0]} test timed out.")
		return False
	if result.returncode != 0:
		print(f"{encoder[0]} failed its test:\n{(result.stdout + result.stderr).strip()[-500:]}")
		return False
	return True


@functools.cache
def h264_encoder() -> list[str]:
	"""The hardware encoder if it works on this machine, otherwise x264."""
	if encoder_works(MPP_ENCODER):
		print("Using the hardware H.264 encoder (mpph264enc).")
		return MPP_ENCODER
	print("Using the software H.264 encoder (x264enc).")
	return X264_ENCODER


def rtsp_sink(host, stream_name) -> list[str]:
	return [
		"queue", "max-size-buffers=2", "leaky=downstream",  # encoder gets its own thread
		"!", *h264_encoder(),
		"!", "h264parse", "config-interval=-1",  # resend SPS/PPS for late joiners
		"!", "rtspclientsink", "protocols=udp", f"location=rtsp://{host}:8554/{stream_name}",
	]


def launch_camera(source, stream_name, host) -> subprocess.Popen:
	"""Start a pipeline publishing `source` to MediaMTX at `host`.

	`host` must be the container's bridge IP, not 127.0.0.1: going through
	docker-proxy rewrites the UDP source port and breaks MediaMTX's check."""
	print(f"{stream_name}: launching")
	return subprocess.Popen(["gst-launch-1.0", *source, "!", *rtsp_sink(host, stream_name)])


def stop_processes(procs):
	for proc in procs:
		if proc.poll() is None:
			proc.terminate()
	for proc in procs:
		try:
			proc.wait(timeout=5)
		except subprocess.TimeoutExpired:
			proc.kill()
			proc.wait()


# ---------------------------------------------------------------------------
# Supervision
# ---------------------------------------------------------------------------


@dataclass
class Pipeline:
	proc: subprocess.Popen
	deadline: float  # counts as stalled if no new data has arrived by then
	last_bytes: int | None = None


class Supervisor:
	"""Keeps one pipeline running per connected camera, restarting any that
	exit or stall, and picking up cameras that appear later."""

	def __init__(self, mediamtx):
		self.mediamtx = mediamtx
		self.pipelines: dict[str, Pipeline] = {}
		self.waiting = False

	def launch_missing(self):
		cameras = discover_cameras()
		if not cameras and not self.pipelines and not self.waiting:
			print("No cameras found; waiting for one to connect...")
		self.waiting = not cameras
		for name, prepare in cameras.items():
			if name not in self.pipelines:
				proc = launch_camera(prepare(), name, self.mediamtx.ip)
				self.pipelines[name] = Pipeline(proc, time.monotonic() + STARTUP_GRACE)

	def reap(self):
		"""Stop and forget pipelines that exited or stalled, so the next scan
		relaunches them if their camera is still there."""
		inbound = self.mediamtx.inbound_bytes()
		now = time.monotonic()
		for name, p in list(self.pipelines.items()):
			if p.proc.poll() is not None:
				print(f"{name}: pipeline exited (code {p.proc.returncode}); will retry.")
			elif inbound is not None and self._stalled(p, inbound.get(name), now):
				print(f"{name}: no data for {STALL_TIMEOUT}s; restarting.")
				stop_processes([p.proc])
			else:
				continue
			del self.pipelines[name]

	@staticmethod
	def _stalled(p, received, now) -> bool:
		if received is not None and received != p.last_bytes:
			p.last_bytes = received
			p.deadline = now + STALL_TIMEOUT
			return False
		return now > p.deadline

	def run(self):
		next_scan = 0.0
		while True:
			if time.monotonic() >= next_scan:
				self.launch_missing()
				next_scan = time.monotonic() + RESCAN_INTERVAL
			time.sleep(1)
			self.reap()

	def stop(self):
		stop_processes([p.proc for p in self.pipelines.values()])
		self.pipelines.clear()


def main():
	mediamtx = MediaMTXContainer()
	supervisor = Supervisor(mediamtx)

	def cleanup():
		supervisor.stop()
		mediamtx.stop()

	atexit.register(cleanup)
	# Convert signals into a normal exit so atexit runs cleanup exactly once.
	signal.signal(signal.SIGINT, lambda *_: sys.exit(0))
	signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))

	h264_encoder()  # pick and report the encoder up front, not on first camera
	mediamtx.start()
	supervisor.run()


if __name__ == "__main__":
	main()
