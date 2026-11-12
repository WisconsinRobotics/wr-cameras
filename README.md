# wr-cameras

Quick proof of concept for streaming using GStreamer & RTSP.

If you are on VSCode, you may need to add the following to your `settings.json`:
```json
"terminal.integrated.env.linux": {
	"GTK_PATH": null,
	"GIO_MODULE_DIR": null
}
```

## Networking: why UDP

The cameras have to stream over limited, lossy links (radio, weak WiFi), so the
RTSP transport is **UDP on purpose**, on both the streamer (`rtspclientsink
protocols=udp`) and the receiver (`rtspsrc protocols=udp`). Don't switch to TCP:
over a lossy link TCP retransmits and stalls, so latency grows without bound and
the video freezes. UDP keeps the latency low and accepts some lost packets.

Because packets do get lost, the pipeline is tuned so that loss stays visible
only briefly:

- **Encoder (`streamer.py`).** `x264enc intra-refresh=true key-int-max=15`
  refreshes the picture as a rolling stripe instead of sending whole keyframes.
  A lost packet smears the image for at most 15 frames (0.5 s at 30 fps), and
  there are no periodic keyframe bursts to overflow the link.
  `vbv-buf-capacity=200` caps bitrate spikes. Lower `bitrate` if the link is
  tighter than about 1.5 Mbit/s per camera.
- **Receiver (`receiver.py`).** `latency=50` gives the jitter buffer 50 ms, so
  packets that arrive late or out of order still get used instead of being
  dropped. `udp-buffer-size` absorbs bursts. Linux caps the buffer at
  `net.core.rmem_max` (about 208 KB by default); to raise the cap, run:
  `sudo sysctl -w net.core.rmem_max=2097152`.

Short horizontal smears that clear on their own are packet loss. Smears that
never clear point to a different problem.
