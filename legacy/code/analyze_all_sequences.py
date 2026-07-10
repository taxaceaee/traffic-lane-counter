import xml.etree.ElementTree as ET
from pathlib import Path

import numpy as np

seqs = {
    "MVI_40181": "/home/ttung05/Desktop/vehicle_counting/datasets/DETRAC/DETRAC-Train-Annotations-XML/DETRAC-Train-Annotations-XML/MVI_40181.xml",
    "MVI_40864": "/home/ttung05/Desktop/vehicle_counting/datasets/DETRAC/DETRAC-Test-Annotations-XML/DETRAC-Test-Annotations-XML/MVI_40864.xml",
    "MVI_40761": "/home/ttung05/Desktop/vehicle_counting/datasets/DETRAC/DETRAC-Test-Annotations-XML/DETRAC-Test-Annotations-XML/MVI_40761.xml",
    "MVI_40712": "/home/ttung05/Desktop/vehicle_counting/datasets/DETRAC/DETRAC-Test-Annotations-XML/DETRAC-Test-Annotations-XML/MVI_40712.xml",
}
for seq, xml_path in seqs.items():
    if not Path(xml_path).exists():
        print(f"Skipping {seq}, xml does not exist at {xml_path}")
        continue
    tree = ET.parse(xml_path)
    root = tree.getroot()
    pts = []
    for frame in root.findall(".//frame"):
        for box in frame.findall(".//box"):
            left = float(box.get("left"))
            top = float(box.get("top"))
            width = float(box.get("width"))
            height = float(box.get("height"))
            x_center = left + width / 2.0
            y_bottom = top + height
            pts.append((x_center, y_bottom))

    pts = np.array(pts)
    print(f"=== {seq} ===")
    print(f"Total count: {len(pts)}")
    if len(pts) > 0:
        pts_top = pts[pts[:, 1] < 250]
        pts_bottom = pts[pts[:, 1] > 450]
        print(f"Top x-range (y<250): {pts_top[:,0].min() if len(pts_top) > 0 else 'N/A'} to {pts_top[:,0].max() if len(pts_top) > 0 else 'N/A'}")
        print(f"Bottom x-range (y>450): {pts_bottom[:,0].min() if len(pts_bottom) > 0 else 'N/A'} to {pts_bottom[:,0].max() if len(pts_bottom) > 0 else 'N/A'}")

        # Simple histogram at bottom
        if len(pts_bottom) > 0:
            hist, bins = np.histogram(pts_bottom[:, 0], bins=8)
            print("Bottom X histogram:")
            for i in range(len(hist)):
                print(f"  {bins[i]:.1f}-{bins[i+1]:.1f}: {hist[i]}")
