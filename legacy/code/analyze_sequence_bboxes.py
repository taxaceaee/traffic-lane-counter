import xml.etree.ElementTree as ET

import numpy as np


def main():
    xml_path = "/home/ttung05/Desktop/vehicle_counting/datasets/DETRAC/DETRAC-Train-Annotations-XML/DETRAC-Train-Annotations-XML/MVI_20011.xml"
    tree = ET.parse(xml_path)
    root = tree.getroot()

    bboxes = []
    for frame in root.findall(".//frame"):
        for box in frame.findall(".//box"):
            # UA-DETRAC format: <box left="..." top="..." width="..." height="..."/>
            left = float(box.get("left"))
            top = float(box.get("top"))
            width = float(box.get("width"))
            height = float(box.get("height"))
            # Bottom-center point of the vehicle bbox
            x_center = left + width / 2.0
            y_bottom = top + height
            bboxes.append((x_center, y_bottom))

    bboxes = np.array(bboxes)
    print(f"Total vehicle detections: {len(bboxes)}")
    print(f"X range: {bboxes[:, 0].min()} to {bboxes[:, 0].max()}")
    print(f"Y range: {bboxes[:, 1].min()} to {bboxes[:, 1].max()}")

    # We can perform simple clustering or bucket count on X to see where the lanes are
    hist, bin_edges = np.histogram(bboxes[:, 0], bins=10)
    print("Histogram of X coordinates (vehicle centers):")
    for i in range(len(hist)):
        print(f"  Bin {bin_edges[i]:.1f} - {bin_edges[i+1]:.1f}: {hist[i]} detections")

if __name__ == "__main__":
    main()
