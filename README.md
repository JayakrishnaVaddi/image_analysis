# Raspberry Pi Image Analysis

This project captures an image from a Raspberry Pi camera or loads a test image,
detects a white 96-well slab, warps it into a top-down view, samples each well,
classifies well colors using HSV thresholds, saves debug artifacts, exports a
JSON record locally, and attempts to upload one document per run to MongoDB.

On the `feature/live_stream` branch, live mode is session-based. When the
external trigger starts this module in live mode, the Raspberry Pi now:

- starts camera-based analysis
- starts non-contact IR polling
- turns on the heating pad
- optionally streams annotated live video to the configured endpoint
- keeps the session active for the full timed window
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
- `live_stream_service.py`: Annotated live-frame analysis and optional HTTP streaming.
- `ir_sensor_service.py`: MLX90614 IR polling service.
- `heating_pad_service.py`: Heating pad relay and status LED control.
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
SESSION_DURATION_SECONDS=720
STREAM_FPS=4
STREAM_JPEG_QUALITY=85
STREAM_RECONNECT_DELAY=2.0
```

Notes:

- `VIDEO_STREAM_ENDPOINT` is optional. If it is blank or missing, the timed
  session still runs and only live streaming is disabled.
- `SESSION_DURATION_SECONDS` defaults to `720` seconds if not set.
- `STREAM_FPS`, `STREAM_JPEG_QUALITY`, and `STREAM_RECONNECT_DELAY` are optional
  stream tuning values.
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
4. Annotated frames are POSTed to `VIDEO_STREAM_ENDPOINT` if configured.
5. The system stays active for `SESSION_DURATION_SECONDS`.
6. Streaming, heating, IR polling, and camera processing stop cleanly.
7. The module returns to idle and is ready for the next trigger.

If a second trigger arrives while a session is already active, it is ignored and
logged instead of starting overlapping hardware activity.

The live annotated stream uses the same annotation pipeline as the saved
`annotated_result.jpg`, and the streamed frame matches the saved annotated
output size.

Live camera mode uses `rpicam-vid` or `libcamera-vid` for the frame stream and
`rpicam-still` or `libcamera-still` for one-shot capture paths that remain in
the project.

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
