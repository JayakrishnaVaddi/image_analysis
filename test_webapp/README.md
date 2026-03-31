# Test WebSocket Video Stream

This folder contains a separate browser test app whose only purpose is to verify the Raspberry Pi WebSocket live stream.

The stream server belongs to the main project codebase in:

- `/home/pi/image_analysis/live_stream_server.py`

This test app is not integrated into the original application UI or workflow.

## What Was Built

- Main-project stream server:
  - `live_stream_server.py`
  - `hardware_control.py`
  - Continuous WebSocket camera stream server in the main repo.
- Separate test web app:
  - `test_webapp/serve_test_app.py`
  - `test_webapp/index.html`
  - `test_webapp/app.js`
  - `test_webapp/styles.css`

## Streaming Format

This implementation is continuous frame-streaming over WebSocket.

It is not H.264/WebRTC or another browser-native encoded video pipeline.

The server continuously reads JPEG frames from the Raspberry Pi camera using the existing project camera streaming helper, then pushes each JPEG frame to connected browsers as binary WebSocket messages.

When the browser sends a start request, the server also:

- turns the heating pad on
- polls the MLX90614 non-contact IR sensor
- sends live temperature updates over the same WebSocket connection
- runs the session for 60 seconds
- turns the heating pad off on timeout, manual stop, disconnect, or failure
- launches `main.py --mode live` after a normal timed session completes

The browser renders those JPEG frames continuously to behave like a live stream.

## Why This Format Was Chosen

- It reuses the existing project camera stream helper safely.
- It keeps changes additive and isolated.
- It avoids periodic HTTP image posting.
- It works in a browser without introducing a larger video stack such as WebRTC.
- It fits the current repo better than a more invasive encoded-video pipeline.

## Ports Used

- WebSocket stream server default: `8765`
- Test web app HTTP server default: `8080`
- WebSocket stream path: `/stream`

## Dependencies Used

Install project dependencies from the repo root:

```bash
cd /home/pi/image_analysis
pip install -r requirements.txt
```

Important dependency added for the stream server:

- `websockets`
- Raspberry Pi hardware support for the MLX90614 and relay binary used by `auto_heat.py`

## How To Run The Main Project Stream Server

From the repo root:

```bash
cd /home/pi/image_analysis
python3 live_stream_server.py
```

Optional arguments:

```bash
python3 live_stream_server.py --host 0.0.0.0 --port 8765 --camera-index 0
```

## How To Run The Test Web App

From the repo root:

```bash
cd /home/pi/image_analysis
python3 test_webapp/serve_test_app.py
```

Then open:

```text
http://localhost:8080
```

Or from another device on the same network:

```text
http://<raspberry-pi-ip>:8080
```

On the page:

1. Enter the Raspberry Pi WebSocket URL if needed, for example `ws://<raspberry-pi-ip>:8765`
   or `ws://<raspberry-pi-ip>:8765/stream`
2. Click `Start Stream`
3. Confirm the live video and temperature readings appear
4. Confirm the session stops automatically after 60 seconds
5. Optionally click `Stop Stream` early and confirm the session shuts down cleanly

## Limitations

- This is continuous JPEG frame-streaming over WebSocket, not true encoded video playback.
- Browser rendering swaps incoming JPEG blobs, which is practical for validation but less efficient than a dedicated video codec pipeline.
- The camera should not be used by multiple processes at the same time.
- This path is intended for manual validation, not as a production browser streaming stack.

## How Original Flow Was Preserved

- The stream server was added as a new main-project module instead of replacing existing runtime flow.
- Heater and IR control were wrapped into `hardware_control.py` using `auto_heat.py` as the reference for relay command usage and MLX90614 read behavior.
- The original analysis CLI was not changed.
- The original camera acquisition path for analysis was not changed.
- The original result generation, Mongo upload, and artifact save flow were not changed.
- The test web app remains separate under `test_webapp/`.
