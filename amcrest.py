import os
import subprocess
import sys

IP = "192.168.1.108"
PORT = 554  # default RTSP port for Amcrest
USER = "admin"
CHANNEL = 1
SUBTYPE = 0  # 0 = main stream (high res), 1 = sub stream (low res)


def gst_quote(value) -> str:
	"""Quote a property value for gst-launch so characters like '!' or spaces
	in it aren't parsed as pipeline syntax."""
	return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def main():
	password = os.environ.get("PASSWORD")
	if not password:
		sys.exit("No PASSWORD set (put it in .env); exiting.")

	url = f"rtsp://{IP}:{PORT}/cam/realmonitor?channel={CHANNEL}&subtype={SUBTYPE}"
	print("Close the video window or press Ctrl+C to exit.")
	try:
		subprocess.run(
			[
				"gst-launch-1.0",
				"rtspsrc",
				f"location={gst_quote(url)}",
				f"user-id={gst_quote(USER)}",
				f"user-pw={gst_quote(password)}",
				"protocols=udp",  # see README "Networking: why UDP"
				"latency=50",
				"!", "decodebin",  # picks the right depayloader/decoder (H.264 or H.265)
				"!", "videoconvert",
				"!", "autovideosink", "sync=false",
			],
			check=False,
		)
	except KeyboardInterrupt:
		pass


if __name__ == "__main__":
	main()
