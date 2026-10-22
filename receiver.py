import subprocess

import requests
import argparse

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
    # Parse args using argparse
    parser = argparse.ArgumentParser(prog='WR Camera Reciever', description='Recieves camera data and displays it from WR Camera Streamer', epilog='')
    parser.add_argument('-i', '--ip', help="the ip of the host (ex. 172.10.0.5)")
    args = parser.parse_args()

    ip = args.ip or STREAMER_IP 
    streams = get_available_streams(ip)
    if not streams:
	    print("No available streams found.")
	    return

    procs = []
    for stream in streams:
	    print(f"Receiving stream: {stream}")
	    procs.append(receive_stream(ip, stream))

    try:
	    for p in procs:
	        p.wait()
    except KeyboardInterrupt:
	    for p in procs:
		    p.terminate()


if __name__ == "__main__":
	main()
