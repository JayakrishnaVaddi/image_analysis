# Raspberry Pi 96-Well Plate Image Analysis System

---

## Table of Contents

1. [System Overview](#system-overview)
2. [Architecture Summary](#architecture-summary)
3. [Entry Points](#entry-points)
4. [File Reference](#file-reference)
   - [Core Application](#core-application)
   - [Support Modules](#support-modules)
   - [Hardware & Peripheral Control](#hardware--peripheral-control)
   - [Configuration & Calibration](#configuration--calibration)
   - [Database Layer](#database-layer)
   - [Utilities & Tools](#utilities--tools)
5. [Data Flow](#data-flow)
6. [Hardware Components](#hardware-components)
7. [Setup & Installation](#setup--installation)
8. [How to Run](#how-to-run)
9. [Configuration Reference](#configuration-reference)
10. [Output Files](#output-files)
11. [Database Schema](#database-schema)

---

## System Overview

This project is a Raspberry Pi–based image analysis system designed to detect and classify the contents of a **96-well plate** (12 rows × 8 columns). It captures a high-resolution image of the plate using the Raspberry Pi camera, undistorts the image using stored camera calibration data, locates the plate slab in the frame, detects each of the 96 wells individually, classifies the color of each well's liquid sample using HSV thresholds, and produces a **binary gene-presence array** representing which wells contain a detectable sample.

Results are saved locally as JSON and image artifacts, and uploaded to **MongoDB Atlas** for downstream consumption by a backend web application. A real-time **WebSocket server** streams live video to a connected frontend client, manages a heating pad to bring the plate to the target temperature (105 °C), controls LED illumination, and automatically triggers the full analysis pipeline at the end of a timed test session.

---

## Architecture Summary

```
Frontend WebSocket Client
        │
        ▼
live_stream_server.py   ◄──── Entry point (WebSocket server)
        │
        ├── Manages session lifecycle (device start → heat → test → stop)
        ├── Streams live MJPEG video frames to client
        ├── Reads temperature from MLX90614 sensor every 1 second
        ├── Controls heater relay and LED ring lights
        └── Spawns main.py as a subprocess when test completes
                │
                ▼
            main.py   ◄──── Analysis pipeline (also runnable standalone)
                │
                ├── Captures still image (rpicam-still) OR loads from disk
                ├── Undistorts image via camera_calibration.json
                ├── Preprocesses (optional crop + Gaussian blur)
                ├── plate_analyzer.py → detects slab, wells, classifies colors
                ├── Fetches gene panel metadata from MongoDB
                ├── Saves images + JSON artifacts to output/run_<timestamp>/
                └── Uploads backend results document to MongoDB Atlas
```

---

## Entry Points

### `live_stream_server.py` — Primary Entry Point

This is the **main entry point for production use**. It starts a WebSocket server on port `8765` that a frontend client connects to. Through this single connection, the server coordinates the entire device lifecycle:

1. Streams live camera video to the client during warm-up
2. Controls the heating pad relay to reach the 105 °C target
3. Triggers a 60-second timed test session
4. Turns LEDs on/off at the correct moments
5. At session end, spawns `main.py` as a subprocess to run the full image analysis pipeline
6. Sends the resulting gene panel data back to the client over WebSocket

**Run with:**
```bash
python live_stream_server.py
python live_stream_server.py --host 0.0.0.0 --port 8765 --camera-index 0
```

---

### `main.py` — Standalone Test / Analysis Script

This is a **standalone CLI script** used to verify the analysis pipeline works correctly without needing to start the server. It runs the full image analysis workflow from the command line — capture or load an image, detect the slab and wells, classify colors, save artifacts, and upload to MongoDB.

> **Note:** `main.py` is also the script that `live_stream_server.py` spawns internally at the end of each test session. In production it is not run manually; it is called automatically by the server.

**Run with:**
```bash
# Capture from the live camera
python main.py --mode live

# Analyze a saved image from disk
python main.py --mode image --image /path/to/plate.jpg

# With optional flags
python main.py --mode live --plate-id my_plate_001 --display --camera-index 0
```

**CLI Arguments:**

| Argument | Description |
|---|---|
| `--mode` | `live` (capture from camera) or `image` (load from disk). Required. |
| `--image` | Path to an image file. Required when `--mode image` is used. |
| `--plate-id` | Plate identifier stored in the MongoDB document. Defaults to the run ID. |
| `--mongo-uri` | MongoDB connection string override. Defaults to `MONGO_URI` in `.env`. |
| `--display` | Open an OpenCV window to visually inspect the annotated result. |
| `--camera-index` | Camera index passed to rpicam tools. Default: `0`. |

---

## File Reference

### Core Application

---

#### `live_stream_server.py`

**Purpose:** WebSocket server that coordinates the entire device session lifecycle.

**Key Responsibilities:**
- Accepts WebSocket connections from the frontend on path `/stream`
- Processes incoming control messages (`start_device`, `run_test`, `stop_device`, `start_session`, `stop_session`)
- Manages session state through the `SessionCoordinator` class
- Runs three concurrent async tasks during a test: temperature control, video streaming, and test timing
- Implements a bang-bang heater controller: heater turns ON below 104 °C, OFF above 106 °C
- Sends real-time JSON telemetry to the client: temperature readings, heater state changes, target-reached events, test progress, and final analysis results
- After a 60-second test completes, spawns `main.py --mode live` as a subprocess and waits for it to finish
- Reads `backend_results.json` from the newest output run directory and sends the full gene panel payload back to the client as an `analysis_complete` WebSocket message

**Key Constants:**

| Constant | Value | Description |
|---|---|---|
| `SESSION_DURATION_SECONDS` | `60` | Test duration in seconds |
| `TARGET_TEMPERATURE_C` | `105.0` | Target plate temperature |
| `HEATER_ON_BELOW_C` | `104.0` | Heater turns on below this |
| `HEATER_OFF_ABOVE_C` | `106.0` | Heater turns off above this |
| `LED_OFF_DELAY_SECONDS` | `5.0` | Delay before LEDs turn off after test ends |
| `STREAM_PATH` | `/stream` | WebSocket endpoint path |

**WebSocket Message Types (server → client):**

| Type | Trigger |
|---|---|
| `info` | On connection |
| `device_started` | When device session is initialized |
| `temperature` | Every 1 second during an active session |
| `heater_state` | When heater relay state changes |
| `target_reached` | When plate first reaches 105 °C |
| `test_started` | When 60-second timer begins |
| `test_completed` | When 60-second timer finishes |
| `test_stopped` | When test is manually stopped early |
| `device_stopped` | When device session ends |
| `device_error` | On fatal hardware or pipeline error |
| `analysis_complete` | After `main.py` finishes; contains gene panel results |

---

#### `main.py`

**Purpose:** End-to-end image analysis pipeline; also serves as the analysis subprocess called by `live_stream_server.py`.

**Key Responsibilities:**
- Acquires an image from the Raspberry Pi camera (`rpicam-still`) or loads one from disk
- Undistorts the image using `camera_calibration.json`
- Applies preprocessing (optional Gaussian blur and proportional crop)
- Instantiates `PlateAnalyzer` and runs the full detection pipeline
- Fetches gene panel metadata from the MongoDB `Gene_panel` collection
- Maps analysis results (96-element binary array) onto gene panel records
- Saves all output files to `output/run_<timestamp>/`
- Uploads the backend gene results document to MongoDB Atlas
- Returns a structured result dictionary

**Output Files Saved Per Run:**

| File | Description |
|---|---|
| `original.jpg` | The raw captured/loaded image |
| `debug_undistorted.jpg` | Image after lens undistortion |
| `analyzed_input.jpg` | The preprocessed analysis input |
| `slab_detection.jpg` | Debug overlay showing slab corners |
| `warped_slab.jpg` | Perspective-corrected slab view |
| `grid_overlay.jpg` | Accepted well candidates overlaid on image |
| `candidate_wells.jpg` | All raw well candidates |
| `labeled_wells.jpg` | Wells labeled with assigned well numbers |
| `sample_regions.jpg` | Color sampling circles overlaid |
| `annotated_result.jpg` | Final result with gene values per well |
| `clean_result.jpg` | Clean color-filled circles on white background |
| `result.jpg` | Ordered canonical grid output |
| `results.json` | Raw binary data payload (`plateId`, `timestamp`, `binaryData`) |
| `mapped_results.json` | Gene panel records merged with detection results |
| `backend_results.json` | Backend-facing payload with `geneName`, `genotypes`, `testResult` per well |

---

### Support Modules

---

#### `plate_analyzer.py`

**Purpose:** Core computer vision module for slab detection, well detection, and color classification.

**Key Responsibilities:**
- Locates the white slab in the image using HSV color thresholding and connected-component analysis
- Fits a quadrilateral around the slab and applies a perspective warp to produce a normalized helper view
- Detects 96 well candidates using a combination of:
  - Adaptive thresholding + saturation masking → contour analysis
  - Hough circle transform (rescue fallback for faint wells)
  - Anchored neighborhood search (fallback when too few wells are detected globally)
- Merges candidates from all detectors, deduplicating by proximity
- Assigns each candidate to a cell in the 12×8 output lattice using k-means clustering on normalized coordinates
- Aligns assigned well centers to a regularized grid (constant row/column spacing)
- Refines each center to the local color centroid inside a small search window
- Classifies each well's color by sampling the mean HSV value in a circular region, then matching against configured color profiles
- Returns an `AnalysisResult` dataclass containing: 96-element gene presence array, well color list, all debug image artifacts, slab corners, and per-well details

**Key Classes:**

| Class | Description |
|---|---|
| `PlateAnalyzer` | Main analysis class; call `.analyze(image)` |
| `AnalysisResult` | Final result returned to `main.py` |
| `WellDetail` | Per-well data: number, label, center, color, gene value |
| `WellCandidate` | A raw detection candidate before assignment |
| `AnalysisArtifacts` | Bundle of all debug images |
| `SlabDetectionError` | Raised when the white slab cannot be found |
| `WellDetectionError` | Raised when too few wells can be assigned |

---

#### `camera_capture.py`

**Purpose:** All camera interaction and image I/O utilities.

**Key Responsibilities:**
- Captures a single still frame by invoking `rpicam-still` (or `libcamera-still` as fallback) as a subprocess
- Streams MJPEG video frames via `rpicam-vid` (or `libcamera-vid`) for the live WebSocket stream
- Applies a stream-only crop and resize to the outgoing video frames (independent of the analysis crop)
- Loads images from disk and validates they are readable
- Provides optional OpenCV display utilities for debugging

**Key Functions:**

| Function | Description |
|---|---|
| `capture_frame(camera_index, scratch_dir)` | Capture a single still image from the Pi camera |
| `iter_live_frames(camera_index)` | Generator that yields raw JPEG bytes for the live stream |
| `load_image(image_path)` | Load an image file from disk |
| `display_image(window_name, image, delay_ms)` | Show an image in an OpenCV window |
| `close_display_windows()` | Close all open OpenCV windows |

---

#### `color_profiles.py`

**Purpose:** HSV color classification profiles for well detection.

**Key Responsibilities:**
- Defines named `ColorProfile` objects, each containing one or more HSV range thresholds, a BGR render color for visualization, and a `gene_value` (0 or 1)
- Only `yellow` (hue 23–38) maps to `gene_value = 1`; all other colors map to `0`
- Used by `plate_analyzer.py` to classify each well's color and derive the binary gene-presence output

**Supported Colors:** white, brown, red, orange, yellow, lime, green, cyan, blue, indigo, violet, pink

---

#### `db_handler.py`

**Purpose:** MongoDB connectivity, document upload, and gene panel data fetching.

**Key Responsibilities:**
- Loads MongoDB credentials from the local `.env` file
- Resolves MongoDB URI, database name, and collection name from environment variables with fallback to defaults
- Uploads one run document to `image_analysis.well_plate_results`
- Fetches gene panel seed records from `Gene_set.Gene_panel` for result mapping
- Provides an `insert_test_document()` utility for Atlas connectivity testing

**Environment Variables:**

| Variable | Default | Description |
|---|---|---|
| `MONGO_URI` | _(required)_ | MongoDB Atlas connection string |
| `MONGO_DB_NAME` | `image_analysis` | Results database name |
| `MONGO_COLLECTION_NAME` | `well_plate_results` | Results collection name |
| `MONGO_SOURCE_DB_NAME` | `Gene_set` | Gene panel source database |
| `MONGO_SOURCE_COLLECTION_NAME` | `Gene_panel` | Gene panel source collection |

---

### Hardware & Peripheral Control

---

#### `hardware_control.py`

**Purpose:** Unified hardware abstraction for the heater relay, LED ring, and IR temperature sensor.

**Key Responsibilities:**
- Controls the **heater relay** by calling the `3relind-rpi/3relind` command-line tool (channel 1)
- Controls the **LED ring lights** via `Led_control/gpio_control.py`
- Reads the object temperature from the **MLX90614** non-contact IR sensor over I2C
- Used exclusively by `live_stream_server.py` through the `HardwareController` class

**Key Methods:**

| Method | Description |
|---|---|
| `turn_heater_on()` | Closes relay channel 1 |
| `turn_heater_off()` | Opens relay channel 1 |
| `turn_leds_on()` | Turns LED ring on |
| `turn_leds_off()` | Turns LED ring off |
| `read_temperature_celsius()` | Reads object temperature from MLX90614 via I2C |

---

#### `auto_heat.py`

**Purpose:** Standalone legacy heater control script. Not used by the server.

**Description:** A self-contained script that reads temperature from the MLX90614 sensor in a loop and directly controls the heater relay and LEDs using the original GPIO pin assignments. This was the original heating controller before it was integrated into `hardware_control.py`. It is kept as a reference and for manual hardware testing.

**Run with:**
```bash
python auto_heat.py
```

---

#### `Led_control/gpio_control.py`

**Purpose:** GPIO LED control using `gpiozero`.

**Description:** Provides `on_led()` and `off_led()` functions that toggle a set of GPIO-connected LED outputs. Used by `hardware_control.py` to control the plate illumination ring. The LED pins are defined in this module.

---

#### `3relind-rpi/3relind`

**Purpose:** Compiled binary for the Sequent Microsystems 3-relay HAT.

**Description:** A pre-built command-line executable that controls the relay HAT attached to the Raspberry Pi GPIO header. The `hardware_control.py` module calls this binary with arguments `0 write 1 on|off` to toggle the heater relay. This binary is provided by the HAT manufacturer and is not Python source.

---

### Configuration & Calibration

---

#### `config.py`

**Purpose:** Central configuration module. All tunable parameters live here.

**Description:** Defines frozen dataclass instances for every subsystem. Importing a config value anywhere in the project means importing from this one file. No magic numbers are scattered across the codebase.

**Configuration Sections:**

| Config Object | Description |
|---|---|
| `PLATE_GEOMETRY` | Slab dimensions, well grid size (12×8), warp target size, active area ratios |
| `DETECTION` | Slab detection thresholds: blur kernel, HSV ranges, morphological kernel sizes, aspect ratio targets |
| `WELL_DETECTION` | Well candidate detection parameters: adaptive threshold, Hough params, radius, cluster counts, scoring thresholds |
| `OUTPUT` | Output directory name, timestamp format, run directory prefix |
| `CAMERA` | Capture resolution, rpicam command names, stream source size, stream output size, framerate |
| `MONGO` | Database/collection names, URI env var names, connection timeout |
| `PREPROCESS_CROP` | Proportional crop applied to input before analysis (enabled by default) |
| `PREPROCESS_SMOOTHING` | Gaussian blur applied before analysis (enabled by default) |
| `STREAM_CROP` | Proportional crop applied only to outgoing live stream video |
| `MANUAL_CROP` | Fixed pixel-coordinate fallback crop for slab detection (disabled by default) |
| `RUN_TIMING` | Heat wait durations for test vs. production modes |

---

#### `camera_calibration.json`

**Purpose:** Camera lens calibration data used to correct barrel/radial distortion.

**Description:** Stores the output of a checkerboard-based camera calibration run. Loaded by `main.py` before every analysis to undistort the captured image. If this file is missing or malformed, the pipeline raises a `CalibrationError` and stops.

**Fields:**

| Field | Description |
|---|---|
| `camera_matrix` | 3×3 intrinsic camera matrix |
| `distortion_coefficients` | Radial/tangential distortion coefficients |
| `image_width` | Width the camera was calibrated at (pixels) |
| `image_height` | Height the camera was calibrated at (pixels) |

---

#### `distorted_images/calib_code.py`

**Purpose:** Utility script to generate `camera_calibration.json` from a set of checkerboard calibration images.

**Description:** Run once when setting up a new camera or after changing the camera lens or mounting. Place at least 10–15 checkerboard images taken from different angles into `distorted_images/calibration_images/`, then run this script. It detects inner corners using OpenCV, solves the camera model, and writes `camera_calibration.json`.

**Run with:**
```bash
cd distorted_images
python calib_code.py
```

---

### Database Layer

---

#### `.env`

**Purpose:** Local environment file for MongoDB credentials.

**Description:** Key-value pairs loaded by `db_handler.py` at runtime. This file is excluded from git via `.gitignore` and must be created manually on each deployment.

**Minimum required content:**
```
MONGO_URI=mongodb+srv://<user>:<password>@<cluster>.mongodb.net/
```

Optional overrides:
```
MONGO_DB_NAME=image_analysis
MONGO_COLLECTION_NAME=well_plate_results
MONGO_SOURCE_DB_NAME=Gene_set
MONGO_SOURCE_COLLECTION_NAME=Gene_panel
```

---

### Utilities & Tools

---

#### `mongo_connectivity_test.py`

**Purpose:** One-shot MongoDB connectivity test.

**Description:** A minimal script that calls `insert_test_document()` from `db_handler.py` to verify the `.env` credentials are correct and the Atlas cluster is reachable. Run this after creating or changing the `.env` file.

**Run with:**
```bash
python mongo_connectivity_test.py
```

---

#### `tests.py`

**Purpose:** Integration and unit test suite.

**Description:** Contains tests covering the major pipeline stages: image preprocessing, slab detection, well detection, color classification, JSON output, and MongoDB upload behavior. Run to verify changes have not broken the analysis pipeline.

**Run with:**
```bash
python -m pytest tests.py
```

---

## Data Flow

```
[Camera] ──rpicam-still──► [main.py]
                                │
                    ┌───────────▼────────────┐
                    │    undistort_image()   │  ◄── camera_calibration.json
                    └───────────┬────────────┘
                                │
                    ┌───────────▼────────────┐
                    │   preprocess_image()   │  ◄── PREPROCESS_CROP, SMOOTHING
                    └───────────┬────────────┘
                                │
                    ┌───────────▼────────────┐
                    │  PlateAnalyzer.analyze │
                    │  ┌─────────────────┐   │
                    │  │  locate_slab()  │   │  ← white mask + connected components
                    │  │  detect_wells() │   │  ← contours + Hough + anchor search
                    │  │  assign_ids()   │   │  ← k-means clustering + lattice fit
                    │  │  classify()     │   │  ← HSV sampling + ColorProfiles
                    │  └─────────────────┘   │
                    └───────────┬────────────┘
                                │
               96-element binary array (0/1 per well)
                                │
              ┌─────────────────┴──────────────────┐
              │                                    │
    [results.json]                    [MongoDB Gene_panel fetch]
    [image artifacts]                              │
                                    ┌──────────────▼──────────────┐
                                    │  build_mapped_results()     │
                                    │  build_backend_results()    │
                                    └──────────────┬──────────────┘
                                                   │
                                   ┌───────────────┴───────────────┐
                                   │                               │
                        [backend_results.json]           [MongoDB Atlas upload]
                                   │
                        [live_stream_server.py reads this
                         and sends "analysis_complete"
                         over WebSocket to client]
```

---

## Hardware Components

| Component | Purpose | Interface |
|---|---|---|
| Raspberry Pi (any model with camera port) | Main compute unit | — |
| Raspberry Pi Camera Module | Captures still images and live video | CSI ribbon cable |
| Sequent Microsystems 3-Relay HAT | Controls heating pad power | GPIO / HAT |
| Heating pad | Brings plate to 105 °C target | Relay channel 1 |
| LED ring / GPIO LEDs | Illuminates the plate during imaging | GPIO pins 5, 17, 22, 23, 26, 27 |
| MLX90614 non-contact IR sensor | Reads plate surface temperature | I2C (SCL/SDA) |

---

## Setup & Installation

**1. Install Python dependencies:**
```bash
cd /home/pi/image_analysis
python -m venv env
source env/bin/activate
pip install -r requirements.txt
```

**2. Configure MongoDB credentials:**
```bash
nano .env
# Add: MONGO_URI=mongodb+srv://<user>:<pass>@<cluster>.mongodb.net/
```

**3. Verify MongoDB connectivity:**
```bash
python mongo_connectivity_test.py
```

**4. Calibrate the camera (first time only or after remounting):**
```bash
cd distorted_images
# Place 10-15 checkerboard photos in calibration_images/
python calib_code.py
cp camera_calibration.json ../
```

**5. Verify hardware (Raspberry Pi only):**
```bash
python auto_heat.py    # tests heater relay and temperature sensor
```

---

## How to Run

### Production (WebSocket server + full device lifecycle)

```bash
source env/bin/activate
python live_stream_server.py
```

The server listens on `ws://0.0.0.0:8765/stream`. Connect a frontend WebSocket client to this address and send control messages to start a device session, run a test, and receive results.

**Optional flags:**
```bash
python live_stream_server.py --host 0.0.0.0 --port 8765 --camera-index 0
```

### Standalone analysis (for testing the pipeline without the server)

```bash
source env/bin/activate

# Live capture from camera
python main.py --mode live

# Analyze an image from disk
python main.py --mode image --image original.jpg

# Show annotated result in an OpenCV window
python main.py --mode image --image original.jpg --display
```

---

## Configuration Reference

All configuration values are in [config.py](config.py). The most commonly adjusted values are:

| Setting | Location | Default | Notes |
|---|---|---|---|
| Well radius (helper-warp pixels) | `WellDetectionConfig.fixed_candidate_radius_px` | `24.0` | Increase if detected circles appear too small |
| Minimum detected wells | `WellDetectionConfig.min_detected_wells` | `56` | Lower if the system refuses to run on partially-visible plates |
| Preprocessing crop | `PreprocessCropConfig` | enabled, ~0.22–0.72 width | Tighten to exclude fixture edges |
| Stream crop | `StreamCropConfig` | enabled, portrait region | Adjust to frame the plate in the live view |
| Camera resolution | `CameraConfig.frame_width/height` | 4056×3040 | Full resolution for analysis |
| Target temperature | `live_stream_server.py: TARGET_TEMPERATURE_C` | `105.0` | Change at the top of the server file |
| Test duration | `live_stream_server.py: SESSION_DURATION_SECONDS` | `60` | Change at the top of the server file |

---

## Output Files

Each run creates a new directory under `output/run_<YYYYMMDDTHHMMSS>/`:

```
output/
└── run_20240531T142301/
    ├── original.jpg                     Raw input image
    ├── debug_undistorted.jpg            After lens correction
    ├── analyzed_input.jpg               After preprocessing crop/blur
    ├── slab_detection.jpg               Slab boundary overlay
    ├── warped_slab.jpg                  Perspective-corrected slab
    ├── candidate_wells.jpg              All raw well candidates
    ├── grid_overlay.jpg                 Accepted candidates on image
    ├── labeled_wells.jpg                Wells with assigned numbers
    ├── sample_regions.jpg               Color sampling circles
    ├── annotated_result.jpg             Gene values per well
    ├── clean_result.jpg                 Color circles on white background
    ├── result.jpg                       Ordered canonical grid
    ├── results.json                     Raw binary array
    ├── mapped_results.json              Gene panel + detection merge
    └── backend_results.json             Backend-facing gene payload
```

---

## Database Schema

### Results Collection (`image_analysis.well_plate_results`)

Document uploaded after each analysis run:

```json
{
  "plateId": "run_20240531T142301",
  "timestamp": "2024-05-31T14:23:01.000Z",
  "genes": [
    {
      "geneName": "BRCA1",
      "genotypes": "A/G",
      "testResult": "yellow"
    },
    {
      "geneName": "TP53",
      "genotypes": "G/G",
      "testResult": "red"
    }
  ]
}
```

`testResult` is `"yellow"` when the well is positive (gene present, binary value = 1) and `"red"` when negative.

### Gene Panel Source Collection (`Gene_set.Gene_panel`)

Each document represents one well on the expected plate layout:

```json
{
  "wellNum": 1,
  "gene": "BRCA1",
  "genotype": "A/G"
}
```

`wellNum` is 1-indexed (1–96) and maps directly to the position in the binary data array.
