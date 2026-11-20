"""Project RGB-aligned depth into the same camera frame as DepthAI spatial detections."""

import numpy as np


def spatial_position(box, depth, intrinsics):
    if depth.ndim != 2:
        raise ValueError('Expected an RGB-aligned depth map')
    x1, y1, x2, y2 = map(float, box)
    if not np.isfinite([x1, y1, x2, y2]).all() or x2 <= x1 or y2 <= y1:
        return None
    height, width = depth.shape
    cx, cy = (x1 + x2) / 2, (y1 + y2) / 2
    # Use the central half of the box, matching the OAK bounding-box scale.
    half_w, half_h = (x2 - x1) / 4, (y2 - y1) / 4
    left, right = max(0, int(cx - half_w)), min(width, int(np.ceil(cx + half_w)))
    top, bottom = max(0, int(cy - half_h)), min(height, int(np.ceil(cy + half_h)))
    if right <= left or bottom <= top:
        return None
    roi = depth[top:bottom, left:right]
    valid = roi[np.isfinite(roi) & (roi >= 250) & (roi <= 10000)]
    if valid.size < max(5, int(roi.size * 0.1)):
        return None
    z = float(np.median(valid)) / 1000.0
    if z <= 0.5:
        return None
    fx, fy = intrinsics[0, 0], intrinsics[1, 1]
    # DepthAI camera coordinates: X right, Y up, Z forward, all in meters.
    return ((cx - intrinsics[0, 2]) * z / fx,
            -(cy - intrinsics[1, 2]) * z / fy, z)
