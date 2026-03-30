# Raspberry Pi Image Analysis

This project captures an image from a Raspberry Pi camera or loads a test image,
detects a white 96-well slab, warps it into a top-down view, samples each well,
classifies well colors using HSV thresholds, saves debug artifacts, exports a
JSON record locally, and attempts to upload one document per run to MongoDB.

On the `feature/live_stream` branch, the Raspberry Pi is intended to run as a
server. The Pi listens for lightweight connection checks and real start-test
requests. When a real test trigger arrives, the Raspberry Pi now:

- starts camera-based live analysis
- starts non-contact IR polling
- starts heating-pad control
- by default, streams annotated live video to the configured WebSocket endpoint
- optionally opens one outbound WebSocket connection to the backend and sends
  annotated frames plus live session telemetry during the session
- keeps the session active for the full timed window
- returns the final result payload after the timed session completes
- optionally sends the final `binaryData` result to the backend as a binary
  WebSocket message before cleanup
- shuts everything down cleanly and returns to idle

The current gene-readout workflow recognizes only `light pink`, `red`, and
`yellow`. Yellow maps to gene present (`1`), while light pink and red map to
gene not present (`0`).

The current configuration assumes the slab is placed vertically, with `12` rows
and `8` columns in the warped top-down view.

## Project Layout

- `main.py`: CLI entry point and end-to-end workflow orchestration.
- `camera_capture.py`: Camera capture and image loading helpers.
- `plate_analyzer.py`: Slab detection, perspective transform, well sampling, and color classification.
- `db_handler.py`: MongoDB persistence with graceful failure handling.
- `config.py`: Tunable thresholds, geometry, preprocessing crop, and fallback crop settings.
- `session_orchestrator.py`: Timed live-session control and duplicate-trigger protection.
- `live_stream_service.py`: Annotated live-frame analysis plus optional HTTP and WebSocket outbound streaming.
- `ir_sensor_service.py`: MLX90614 IR polling service.
- `heating_pad_service.py`: Heating pad relay and status LED control.
- `command_handler.py`: Request router for Pi server actions such as health checks, timed test triggers, and legacy one-shot color detection.
- `socket_server.py`: Standalone newline-delimited JSON TCP server on port `5000`.
- `test_web_app/`: Local harness that verifies Pi connectivity, starts a timed test on the Pi server, receives live frames, and displays the final result.
- `output/`: Generated images and JSON result files.

## Manual Virtual Environment Setup

This project does not create a virtual environment automatically. If you want
to create one manually:

```bash
cd /home/pi/image_analysis
python3 -m venv .venv
source .venv/bin/activate
```

## Manual Dependency Installation

After activating your environment, install dependencies manually:

```bash
cd /home/pi/image_analysis
pip install -r requirements.txt
```

If you do not want to use a virtual environment, you can install the same
packages into your system Python manually instead.

For Raspberry Pi hardware runs, make sure the environment also has the packages
needed by the hardware services, including:

- `opencv-python`
- `pymongo`
- `gpiozero`
- `adafruit-circuitpython-mlx90614`
- `adafruit-blinka`

The relay executable used by the heating-pad service is expected at:

```bash
/home/pi/image_analysis/3relind-rpi/3relind
```

## Environment Setup

Create or update `/home/pi/image_analysis/.env` with the values needed for your
deployment:

```dotenv
MONGO_URI=<OPTIONAL_MONGODB_CONNECTION_STRING>
MONGO_COLLECTION_NAME=plate_results
VIDEO_STREAM_ENDPOINT=
VIDEO_STREAM_WS_ENDPOINT=
SESSION_DURATION_SECONDS=720
STREAM_FPS=4
STREAM_JPEG_QUALITY=85
STREAM_RECONNECT_DELAY=2.0
```

Notes:

- `VIDEO_STREAM_WS_ENDPOINT` is optional. If it is blank or missing, the timed
  session falls back to HTTP streaming if `VIDEO_STREAM_ENDPOINT` is configured.
- `VIDEO_STREAM_ENDPOINT` is optional and is now a fallback / compatibility
  transport used only when no WebSocket endpoint is available.
- `SESSION_DURATION_SECONDS` defaults to `720` seconds if not set.
- `STREAM_FPS`, `STREAM_JPEG_QUALITY`, and `STREAM_RECONNECT_DELAY` are optional
  stream tuning values shared by the HTTP and WebSocket frame-delivery paths.
- MongoDB upload is still optional. If `MONGO_URI` is not configured, local JSON
  and image artifacts are still saved.

## Running In Live Camera Mode

```bash
cd /home/pi/image_analysis
python3 main.py --mode live --camera-index 0
```

Optional flags:

- `--mongo-uri mongodb+srv://...` to override the environment value
- `--display`

Live camera mode is now a timed session. One trigger starts one active session:

1. IR polling starts.
2. Heating pad control starts.
3. Live annotated frame processing starts.
4. Session-aware WebSocket messages are sent to `VIDEO_STREAM_WS_ENDPOINT` if configured.
6. The WebSocket stream carries:
   - annotated JPEG frames
   - latest IR temperature
   - time remaining
   - session lifecycle events
   - final `binaryData` as a binary WebSocket message at session completion
7. If no WebSocket endpoint is available, annotated frames fall back to
   `VIDEO_STREAM_ENDPOINT` if it is configured.
8. The system stays active for `SESSION_DURATION_SECONDS`.
9. Streaming, heating, IR polling, and camera processing stop cleanly.
10. The module returns to idle and is ready for the next trigger.

Abort and failure cleanup behavior:

- If the timed session ends normally, cleanup still stops streaming, turns the
  heating pad off, stops IR polling, and returns the system to idle.
- If the timed session fails internally, raises an exception, or is aborted
  early, the same cleanup path still runs.
- The heating pad shutdown guarantee is implemented through the existing
  `finally` cleanup path, which calls the heater service's `stop()` method and
  forces the relay off before returning to idle.

If a second trigger arrives while a session is already active, it is ignored and
logged instead of starting overlapping hardware activity.

The live annotated stream uses the same annotation pipeline as the saved
`annotated_result.jpg`, and the streamed frame matches the saved annotated
output size.

Live camera mode uses `rpicam-vid` or `libcamera-vid` for the frame stream and
`rpicam-still` or `libcamera-still` for one-shot capture paths that remain in
the project.

## Local Test Web App

The local `test_web_app` in this folder now acts like a small external client
for the Pi server. It supports only the Pi-side integration behaviors needed for
this workflow:

1. verify Pi connectivity
2. send a real start-test trigger
3. show live video during the timed session
4. abort a running timed session safely
5. show the final result returned by the Pi server
6. show live IR temperature updates
7. show live remaining-time updates

Start the self-contained test web app:

```bash
cd /home/pi/image_analysis
python3 test_web_app/server.py --pi-host 127.0.0.1 --pi-port 5000 --public-host 127.0.0.1
```

Open `http://127.0.0.1:8081/` in a browser.

Usage:

- Click `Check Pi Connection` to send a lightweight `health` request.
- Click `Start Test Session` to send a real `start_test` request to the Pi server.
- Click `Abort Test Session` to send `abort_test` to the Pi server while a timed
  session is running.
- By default, the harness starts its own local WebSocket receiver and gives that
  receiver URL to the Pi as the session `websocketEndpoint`.
- While the session is running, the page receives and displays the Pi's
  annotated WebSocket video frames.
- The harness also polls the Pi `status` action while the test is active so the
  page can show:
  - current session state
  - session id
  - remaining time
  - latest IR temperature
- The harness remains self-contained inside `test_web_app/`. It is only a local
  receiver/viewer for Pi-session testing and is not part of the production
  backend architecture.
- The page refreshes the latest live frame and shows the final JSON result once
  the timed session finishes or aborts.
- If the local WebSocket receiver cannot run because the `websockets` package is
  unavailable, the harness falls back to the existing HTTP frame path so it
  stays usable for local testing.
- During an active session, expect the live frame area to update, the status to
  move through `starting` / `running`, the remaining time to count down, and the
  IR temperature field to refresh as new sensor readings arrive.
- On normal completion, the page keeps the latest frame, shows `completed`, and
  renders the final result JSON.
- On abort, the page shows `aborting` and then `aborted` once the Pi returns the
  final session response.
- On failure, the page keeps the existing controls and surfaces the latest error
  text so the harness remains useful for debugging.

Arguments:

- `--host`: bind address for the web app
- `--port`: bind port for the web app
- `--ws-port`: bind port for the local WebSocket receiver. Defaults to
  `--port + 1`
- `--pi-host`: host name or IP address of the Pi TCP server
- `--pi-port`: port of the Pi TCP server
- `--camera-index`: camera index forwarded to the Pi start-test request
- `--public-host`: host name or IP address that the Pi should use to reach the
  harness HTTP and WebSocket receivers

Assumptions and limitations:

- The `start_test` socket request remains open until the timed session finishes.
- Aborting a session requests early shutdown but still uses the normal cleanup
  path before the session response is returned.
- The harness is WebSocket-first by default.
- The harness remains standalone and self-contained; it is not coupled to the
  production backend/webapp.
- Only one timed session can run at a time.
- `main.py --mode live` still works as a direct CLI entry point for local runs,
  but `socket_server.py` is now the intended integration surface for external
  step-4 test triggers.

## What Changed

- Connected the Pi TCP server to the existing timed session orchestrator.
- Added a lightweight `health` action with no hardware side effects.
- Added a lightweight `status` action for live session metadata, including
  remaining time and the latest IR temperature.
- Added a real `start_test` action that runs the full internal timed workflow.
- Added an `abort_test` action that stops an in-flight timed session early while
  preserving the same cleanup behavior.
- Added best-effort server-shutdown cleanup so the heating pad is forced off if
  the Pi server process stops through normal shutdown paths.
- Kept live streaming on the existing annotated-frame pipeline.
- Added optional outbound WebSocket session streaming on top of the existing
  annotated-frame and session-orchestration path.
- Updated the local `test_web_app` to check connectivity, start the remote test,
  abort a remote test, display live frames, and show the final result returned
  by the Pi.

## Pi Server Architecture

The Raspberry Pi now acts as a listening TCP server for two primary external
behaviors:

1. connection verification
2. real start-test trigger
3. explicit abort for a running timed session

The server stays idle until a request arrives. A connection check is
lightweight and has no hardware side effects. A real `start_test` request runs
the existing timed live-session workflow and returns the final result only after
the session completes.

High-level flow:

1. The Pi runs `socket_server.py` and waits for newline-delimited JSON.
2. An external client sends `{"action":"health"}` to verify reachability.
3. When the user starts the real test, the client sends `{"action":"start_test", ...}`.
4. The Pi validates the request and calls the existing timed session orchestrator.
5. The orchestrator starts IR polling, heating-pad control, live camera analysis,
   and outbound WebSocket streaming by default.
6. The session stays active for `SESSION_DURATION_SECONDS` and cleans up normally.
7. The Pi returns the final session result JSON to the requesting client.
8. The Pi returns to idle and is ready for the next trigger.

Abort and sudden-stop safety:

- A separate `abort_test` request can be sent while a timed session is active.
- The abort request does not kill the server process. It signals the existing
  orchestrator to end the session early.
- The session still exits through the same cleanup path, which stops live
  streaming, turns the heating pad off, stops IR polling, and clears the active
  session state before future requests are accepted.
- If the Pi server process itself is shut down through normal shutdown paths
  such as `KeyboardInterrupt`, the server now performs best-effort shutdown
  cleanup as well.
- That server-shutdown cleanup requests active session shutdown, stops the live
  stream and IR polling when they are active, and forces the heating-pad relay
  off as a final safety step.

This design reuses the existing session engine instead of creating a second,
parallel workflow.

## Running The Pi Server

Start the Pi server:

```bash
cd /home/pi/image_analysis
python3 socket_server.py
```

Optional flags:

- `--host 0.0.0.0`
- `--port 5000`
- `--camera-index 0`

Request/response model:

- The socket server expects one JSON object per line.
- Each response is also one JSON object per line.
- Malformed JSON or unsupported request shapes return an error response without
  stopping the server.
- One failed request does not stop the server.

Connection verification request:

```json
{"action":"health"}
```

Connection verification response example:

```json
{
  "status":"success",
  "server":"raspberry-pi-image-analysis",
  "session_active":false,
  "supported_actions":["health","status","start_test","abort_test","detect_colors"]
}
```

Live session status request:

```json
{"action":"status"}
```

Live session status response example while a session is running:

```json
{
  "status":"success",
  "server":"raspberry-pi-image-analysis",
  "session_active":true,
  "session_id":"session_1234567890abcdef",
  "session_state":"running",
  "remaining_seconds":412,
  "latest_ir_temperature_c":101.5,
  "abort_requested":false
}
```

Status behavior:

- `remaining_seconds` is calculated from the real timed-session deadline used by
  the orchestrator.
- `latest_ir_temperature_c` comes from the active `IRSensorService` polling
  thread used by the running session.
- When no session is running, status reports `session_active: false`,
  `session_state: "idle"`, and `remaining_seconds` / `latest_ir_temperature_c`
  as `null`.

Real start-test request:

```json
{
  "action":"start_test",
  "cameraIndex":0,
  "streamEndpoint":"http://127.0.0.1:8081/api/live-frame",
  "websocketEndpoint":"ws://127.0.0.1:9000/pi-session-stream"
}
```

Real start-test response behavior:

- The response is delayed until the timed session completes.
- While the session is running, the Pi first tries to stream over
  `websocketEndpoint` if it is provided.
- If no request-level `websocketEndpoint` is present, the Pi next tries
  `VIDEO_STREAM_WS_ENDPOINT`.
- Only if neither WebSocket endpoint is available does the Pi fall back to
  `streamEndpoint`, then `VIDEO_STREAM_ENDPOINT`, for HTTP frame posting.
- If a WebSocket endpoint is configured but unreachable, the Pi keeps the timed
  session running, continues retrying the WebSocket sender, and still preserves
  the normal cleanup path.
- When the session finishes, the response includes the final session result and
  any persisted analysis payload returned by the existing workflow.

Streaming precedence:

1. `websocketEndpoint` from `start_test`
2. `VIDEO_STREAM_WS_ENDPOINT`
3. `streamEndpoint` from `start_test`
4. `VIDEO_STREAM_ENDPOINT`

WebSocket message behavior:

- The Pi is the producer and the backend is expected to act as the WebSocket
  server.
- JSON text messages are used for session lifecycle and telemetry updates.
- Binary WebSocket messages are used for annotated JPEG frames and the final
  `binaryData` payload.
- Each binary WebSocket message is packed as:
  - 4-byte big-endian header length
  - UTF-8 JSON metadata header
  - raw binary payload
- Annotated frame messages use `type: "annotated_frame"` and carry JPEG bytes.
- Telemetry updates use `type: "session_status"` and include:
  - `session_id`
  - `timestamp`
  - `session_state`
  - `remaining_seconds`
  - `ir_temperature_c`
  - `abort_requested`
- Final-result messages use `type: "final_result"` and carry the 96-value
  `binaryData` payload as raw bytes where each byte is either `0` or `1`.

Compatibility and fallback notes:

- WebSocket streaming is now the default live-session transport.
- The existing `streamEndpoint` HTTP path is still available only as fallback /
  compatibility and was not removed.
- The existing timed-session request / response model is unchanged.
- If the WebSocket dependency is not installed, the Pi logs a warning and the
  timed session continues without WebSocket delivery.
- Frame-level slab-detection failures still skip only the affected frame and do
  not stop the session.
- Cleanup and safety behavior are unchanged: normal completion, abort, internal
  failure, and server shutdown still stop streaming, stop IR polling, and force
  the heating pad off through the existing cleanup paths.

Busy-session response example:

```json
{"status":"busy","message":"A timed session is already active"}
```

Abort-session request:

```json
{"action":"abort_test"}
```

Abort-session response example:

```json
{"status":"success","message":"Abort requested; timed session cleanup is in progress"}
```

When the blocked `start_test` request returns after an abort, its final response
uses `status: "aborted"` and includes the session payload collected before the
cleanup completed.

Legacy one-shot detect-colors request:

```json
{"wells":["test"]}
```

That request still captures one frame and returns a short color response for
compatibility, but it does not start the timed session or activate heating/IR
services.

For Raspberry Pi cameras that use a 4:3 sensor, keep `frame_width` and
`frame_height` in `config.py` at a 4:3 ratio such as `4056x3040` or
`2028x1520` if you want the full visible area. A 16:9 setting like
`1920x1080`, or swapped values like `2160x4096`, will crop or distort the
captured field of view.

## Running In Image Mode

```bash
cd /home/pi/image_analysis
python3 main.py --mode image --image /path/to/test.jpg
```

## Output Files

Each run writes files into its own folder inside `output/`, for example
`output/run_20260323T073603/`.

- Original image
- Cropped analysis input image
- Slab detection image
- Warped slab image
- Grid overlay image
- Annotated result image
- Clean grid-only result image
- JSON results

For a live timed session, the saved result comes from a successful annotated
live-analysis frame so the same session camera pipeline is reused for both the
streamed overlay and the persisted artifacts.

The stored and uploaded payload contains only:

- `plateId`
- `binaryData`
- `timestamp`

MongoDB Atlas notes:

- Set `MONGO_URI` in `/home/pi/image_analysis/.env` for normal runs.
- Optional overrides in the same `.env` file:
  - `MONGO_DB_NAME`
  - `MONGO_COLLECTION_NAME`
- `--mongo-uri` is only an override and does not need to be hardcoded into the codebase.
- The Atlas result document contains only `plateId`, `binaryData`, and `timestamp`.

## MongoDB Connectivity Test

Create or update `/home/pi/image_analysis/.env` with:

```dotenv
MONGO_URI=<PASTE_CONNECTION_STRING_HERE>
MONGO_DB_NAME=image_analysis_test
MONGO_COLLECTION_NAME=plate_results
```

Then run the local connectivity test:

```bash
cd /home/pi/image_analysis
python3 mongo_connectivity_test.py
```

That script:

- loads `.env` from the `image_analysis` folder
- connects to MongoDB using `MONGO_URI`
- inserts a small test document
- logs success or failure clearly
- prints the inserted document ID on success

If slab detection fails, the original image is still saved so the failure can be
reviewed later.

## Tuning HSV Thresholds

HSV color thresholds live in `config.py` under `HSV_THRESHOLDS`.

Guidelines for tuning:

1. Capture a representative image and inspect the generated warped slab.
2. Look at the average HSV values in the JSON output for known wells.
3. Adjust the `lower` and `upper` HSV bounds for each color class.
4. Re-run analysis and compare classification results.

OpenCV HSV ranges are:

- Hue: `0-179`
- Saturation: `0-255`
- Value: `0-255`

## Tuning Grid Placement

If the warped slab includes label gutters or blank borders, tune the active
well area in `PLATE_GEOMETRY` inside `config.py`:

- `active_left_ratio`
- `active_top_ratio`
- `active_right_ratio`
- `active_bottom_ratio`

These ratios define the usable well grid area inside the warped slab. The green
inner rectangle in `grid_overlay.jpg` shows the exact region used for well
sampling.

The circle size shown in `grid_overlay.jpg` and `clean_result.jpg` is controlled
separately from the sampling radius, so the visualization can be larger without
changing the grid layout or well centers.

## Manual Crop Fallback

If contour detection is unreliable for your setup, enable the fallback crop in
`config.py`:

```python
MANUAL_CROP = ManualCropConfig(
    enabled=True,
    top_left=(100, 100),
    bottom_right=(1100, 800),
)
```

When enabled, the analyzer uses the configured rectangle if contour detection
does not find a suitable slab boundary.

## Preprocessing Crop

If the slab occupies only part of the camera view, tune `PREPROCESS_CROP` in
`config.py` to crop the input before detection. The saved `analyzed_input.jpg`
file shows the exact image sent into the slab detector.

## MongoDB Behavior

The application attempts to upload one document per run to:

- Database: `image_analysis`
- Collection: `well_plate_results`

If MongoDB is unavailable or the upload fails, the error is logged and the JSON
file remains saved locally in `output/`.

## Live Session Logging

The live session flow logs:

- trigger received
- session start and end
- heating pad on and off
- IR sensor start and stop
- streaming enabled or disabled
- live frame send failures and retries
- cleanup completion

Streaming failures do not stop the timed session. Cleanup still runs at the end
of the session window.

## Troubleshooting

- Camera cannot open: confirm the camera is enabled and that the correct `--camera-index` is being used.
- Camera capture fails: confirm the camera is enabled, that `rpicam-hello` works, and that the correct `--camera-index` is being used.
- If the image looks zoomed in, clipped, or oddly framed, use a native 4:3 size in `config.py`. For IMX477, `4056x3040` gives the most context for slab detection and `2028x1520` is the faster lower-resolution option.
- Image mode fails: make sure the `--image` path exists and is readable.
- Slab detection fails: increase contrast in lighting, tune detection values in `config.py`, or enable manual crop fallback.
- Colors are misclassified: inspect JSON `avg_hsv` values and update `HSV_THRESHOLDS` in `config.py`.
- MongoDB upload fails: verify the URI, server availability, and network access. Local JSON output should still be preserved.
- Live stream fails: confirm `rpicam-vid` or `libcamera-vid` is available and the camera can be opened by the Raspberry Pi camera tools.
- Display windows do not appear: avoid using `--display` in headless environments unless a GUI session is available.

## Test Web App

A small test harness lives in `/home/pi/image_analysis/test_web_app`.

Run it with:

```bash
cd /home/pi/image_analysis
./env/bin/python test_web_app/server.py --port 8081
```

Open `http://<pi-host>:8081/` in a browser. The top-right button starts the real
`main.py --mode live` session flow, and the centered panel shows the live
annotated stream received by the test app during that session.
