ARCH := $(shell dpkg --print-architecture 2>/dev/null)

.PHONY: setup setup-apt setup-aravis setup-tools setup-camera-net \
	aravis-install aravis-uninstall stream receive amcrest hotspot uwnet

# ---------------------------------------------------------------------------
# Setup: `make setup` does everything for the current machine (laptop or Pi).
# Each step is safe to re-run.
# ---------------------------------------------------------------------------

setup: setup-apt setup-aravis setup-tools setup-camera-net
	@echo "Setup complete ($(ARCH))."

GST_PACKAGES = libgstreamer1.0-dev libgstreamer-plugins-base1.0-dev \
	libgstreamer-plugins-bad1.0-dev gstreamer1.0-plugins-base \
	gstreamer1.0-plugins-good gstreamer1.0-plugins-bad gstreamer1.0-plugins-ugly \
	gstreamer1.0-libav gstreamer1.0-tools gstreamer1.0-x gstreamer1.0-alsa \
	gstreamer1.0-gl gstreamer1.0-gtk3 gstreamer1.0-qt5 gstreamer1.0-pulseaudio \
	gstreamer1.0-rtsp

setup-apt:
	sudo apt update
	sudo apt install -y $(GST_PACKAGES) curl dnsmasq-base

# On arm64 (the Pi) the distro Aravis is too old, so use our CI-built .deb.
# Elsewhere (the laptop, Ubuntu 24.04) the distro one is new enough.
ifeq ($(ARCH),arm64)
setup-aravis: aravis-install
else
setup-aravis:
	sudo apt install -y aravis-tools aravis-tools-cli gir1.2-aravis-0.8
endif

# Docker runs MediaMTX; uv runs the Python scripts.
setup-tools:
	@command -v docker >/dev/null || sudo apt install -y docker.io
	@groups | grep -qw docker || { sudo usermod -aG docker $$USER; \
		echo "Added $$USER to the docker group: log out and back in."; }
	@command -v uv >/dev/null || curl -LsSf https://astral.sh/uv/install.sh | sh
	PATH="$$HOME/.local/bin:$$PATH" uv sync  # uv's installer puts it here

# Pi only: share the camera Ethernet port (DHCP for the Triton) on its own
# subnet, separate from the laptop hotspot's 10.42.0.0/24.
CAMERA_IFACE ?= enP3p49s0
CAMERA_SUBNET ?= 10.43.0.1/24
ifeq ($(ARCH),arm64)
setup-camera-net:
	@if nmcli -t -f NAME con show | grep -qx lucid-vision; then \
		echo "lucid-vision connection already exists."; \
	else \
		sudo nmcli con add type ethernet ifname $(CAMERA_IFACE) con-name lucid-vision \
			ipv4.method shared ipv4.addresses $(CAMERA_SUBNET) \
			ipv6.method disabled connection.autoconnect-priority 10; \
	fi
	-sudo nmcli con up lucid-vision
else
setup-camera-net:
	@echo "Not arm64: skipping camera network setup (the laptop uses its own sharing)."
endif

# ---------------------------------------------------------------------------
# Aravis for the Pi. Ubuntu 22.04's 0.8.20 turns a Triton's "generic error"
# GVCP reply into a write_memory timeout; 0.8.30 handles it. The .deb is built
# by .github/workflows/aravis-arm64.yml (run it from the Actions tab) and
# replaces the distro libaravis/aravis-tools-cli packages.
# ---------------------------------------------------------------------------

ARAVIS_VERSION ?= 0.8.30
ARAVIS_DEB = aravis_$(ARAVIS_VERSION)_arm64.deb
ARAVIS_URL = https://github.com/WisconsinRobotics/wr-cameras/releases/download/aravis-$(ARAVIS_VERSION)/$(ARAVIS_DEB)

aravis-install:
	@if dpkg -s aravis-local 2>/dev/null | grep -qx "Version: $(ARAVIS_VERSION)"; then \
		echo "Aravis $(ARAVIS_VERSION) already installed."; \
	else \
		curl -fL -o /tmp/$(ARAVIS_DEB) $(ARAVIS_URL) && \
		sudo apt install -y /tmp/$(ARAVIS_DEB) && \
		rm -rf ~/.cache/gstreamer-1.0; \
	fi
	@gst-inspect-1.0 aravissrc | grep -E "Filename|Version"

aravis-uninstall:  # back to the distro packages
	sudo apt remove -y aravis-local
	sudo apt install -y aravis-tools-cli libaravis-0.8-0
	rm -rf ~/.cache/gstreamer-1.0

# ---------------------------------------------------------------------------
# Running
# ---------------------------------------------------------------------------

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

uwnet:
	sudo nmcli dev wifi rescan
	sudo nmcli dev wifi connect "UWNet"
