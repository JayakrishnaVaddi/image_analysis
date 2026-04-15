#!/usr/bin/env python3
import cv2
import json
import glob
import os
import sys
import numpy as np

# ====== USER SETTINGS ======
# Number of INNER corners in the checkerboard pattern
# Example: a 10x7 square board often has 9x6 inner corners
CHECKERBOARD = (8, 6)

# Physical size of one square on the printed checkerboard
# Unit can be mm, cm, anything consistent
SQUARE_SIZE = 1.0

# Folder containing calibration images
IMAGE_GLOB = "calibration_images/*.jpg"

# Output file expected by your pipeline
OUTPUT_JSON = "camera_calibration.json"
# ===========================


def main() -> int:
    image_paths = sorted(glob.glob(IMAGE_GLOB))
    if not image_paths:
        print(f"[ERROR] No images found matching: {IMAGE_GLOB}")
        return 1

    # 3D real-world checkerboard points
    objp = np.zeros((CHECKERBOARD[0] * CHECKERBOARD[1], 3), np.float32)
    objp[:, :2] = np.mgrid[0:CHECKERBOARD[0], 0:CHECKERBOARD[1]].T.reshape(-1, 2)
    objp *= SQUARE_SIZE

    objpoints = []  # 3D points in real world
    imgpoints = []  # 2D points in image

    image_size = None
    success_count = 0

    print(f"[INFO] Found {len(image_paths)} image(s)")
    print(f"[INFO] Looking for checkerboard inner corners: {CHECKERBOARD}")

    # Termination criteria for corner refinement
    criteria = (
        cv2.TERM_CRITERIA_EPS + cv2.TERM_CRITERIA_MAX_ITER,
        30,
        0.001,
    )

    for path in image_paths:
        img = cv2.imread(path)
        if img is None:
            print(f"[WARN] Could not read: {path}")
            continue

        gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)

        if image_size is None:
            image_size = gray.shape[::-1]  # (width, height)

        flags = (
            cv2.CALIB_CB_ADAPTIVE_THRESH
            + cv2.CALIB_CB_NORMALIZE_IMAGE
        )

        found, corners = cv2.findChessboardCornersSB(gray, CHECKERBOARD, flags=cv2.CALIB_CB_NORMALIZE_IMAGE)

        if not found:
            print(f"[WARN] Checkerboard not detected: {path}")
            continue

        corners_refined = cv2.cornerSubPix(
            gray,
            corners,
            winSize=(11, 11),
            zeroZone=(-1, -1),
            criteria=criteria,
        )

        objpoints.append(objp)
        imgpoints.append(corners_refined)
        success_count += 1
        print(f"[OK] Detected checkerboard: {path}")

    if success_count < 8:
        print(f"[ERROR] Only {success_count} usable image(s) found.")
        print("[ERROR] Capture at least 10-15 good checkerboard images from different angles.")
        return 1

    print(f"[INFO] Running calibration using {success_count} valid image(s)...")

    ret, camera_matrix, dist_coeffs, rvecs, tvecs = cv2.calibrateCamera(
        objpoints,
        imgpoints,
        image_size,
        None,
        None,
    )

    if not ret:
        print("[ERROR] Calibration failed.")
        return 1

    # Reprojection error
    total_error = 0.0
    for i in range(len(objpoints)):
        projected_imgpoints, _ = cv2.projectPoints(
            objpoints[i], rvecs[i], tvecs[i], camera_matrix, dist_coeffs
        )
        error = cv2.norm(imgpoints[i], projected_imgpoints, cv2.NORM_L2) / len(projected_imgpoints)
        total_error += error

    mean_error = total_error / len(objpoints)

    result = {
        "camera_matrix": camera_matrix.tolist(),
        "distortion_coefficients": dist_coeffs.flatten().tolist(),
        "image_width": int(image_size[0]),
        "image_height": int(image_size[1]),
        "checkerboard_inner_corners": [int(CHECKERBOARD[0]), int(CHECKERBOARD[1])],
        "square_size": float(SQUARE_SIZE),
        "num_images_used": int(success_count),
        "mean_reprojection_error": float(mean_error),
    }

    with open(OUTPUT_JSON, "w", encoding="utf-8") as f:
        json.dump(result, f, indent=2)

    print("\n[INFO] Calibration complete")
    print(f"[INFO] Saved to: {OUTPUT_JSON}")
    print(f"[INFO] Images used: {success_count}")
    print(f"[INFO] Mean reprojection error: {mean_error:.6f}")
    print("\nCamera Matrix:")
    print(camera_matrix)
    print("\nDistortion Coefficients:")
    print(dist_coeffs.flatten())

    return 0


if __name__ == "__main__":
    sys.exit(main())