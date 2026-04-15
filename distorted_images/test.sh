#!/usr/bin/env bash
set -euo pipefail

OUTPUT_DIR="calibration_images"
COUNT=30

FRAME_WIDTH=4056
FRAME_HEIGHT=3040
TIMEOUT_MS=1500

# Match your config's command candidates
if command -v rpicam-still >/dev/null 2>&1; then
  CAM_CMD="rpicam-still"
elif command -v libcamera-still >/dev/null 2>&1; then
  CAM_CMD="libcamera-still"
else
  echo "Error: neither rpicam-still nor libcamera-still was found."
  exit 1
fi

mkdir -p "$OUTPUT_DIR"

echo "Using camera command: $CAM_CMD"
echo "Saving images to: $OUTPUT_DIR"
echo "Resolution: ${FRAME_WIDTH}x${FRAME_HEIGHT}"
echo

for i in $(seq -w 18 "$COUNT"); do
  FILE="${OUTPUT_DIR}/cal_${i}.jpg"

  echo "Capture ${i}/${COUNT}: $FILE"
  "$CAM_CMD" \
    --output "$FILE" \
    --timeout "$TIMEOUT_MS" \
    --width "$FRAME_WIDTH" \
    --height "$FRAME_HEIGHT" \
    --nopreview

  echo "Saved: $FILE"
  echo "Reposition the checkerboard slightly, then press Enter for next image..."
  read -r
done

echo
echo "Done. Captured $COUNT calibration images in $OUTPUT_DIR"