"""Interactive synthetic face. All coordinates and feature sizes are source pixels."""

import math

import cv2
import numpy as np


SIMULATION_HINT = (
    "Source view with Manipulate simulation enabled: left-click/drag inside an eye moves pupils; drag elsewhere moves "
    "the face; right-click blinks for 350 ms. Shift-click captures a template. "
    "Hold Ctrl for normal pupil/CR picking and ROI/search-window dragging."
)


class Simulation:
    """Stable facial texture for template matching; gaze never moves the face/CRs."""

    def __init__(self, width, height):
        self.width, self.height = width, height
        # Keep the left eye in the default 320 x 240 ROI at 640 x 480.
        self.eyes = ((width * .25, height * .25),
                     (width * .25 + 200, height * .25))
        self.face = np.full((height, width, 3), 145, np.uint8)
        x, y = self.eyes[0]
        center = (round(x + 100), round(y + 30))
        cv2.ellipse(self.face, center, (152, 106), 0, 0, 360, (110, 110, 110), -1)
        for ex, ey in self.eyes:
            cx, cy = round(ex), round(ey)
            side = -1 if ex == x else 1
            # Ear tufts, pale eye surround and asymmetric fixed markings.
            cv2.ellipse(self.face, (cx + side * 53, cy + 8), (24, 61),
                        side * 15, 0, 360, (172, 172, 172), -1)
            cv2.ellipse(self.face, (cx, cy), (51, 37), 0, 0, 360, (166, 166, 166), -1)
            cv2.ellipse(self.face, (cx, cy), (44, 25), 0, 0, 360, (95, 95, 95), -1)
            cv2.line(self.face, (cx - 30, cy - 30), (cx + 21, cy - 34), (65, 65, 65), 3)
            cv2.circle(self.face, (cx + side * 37, cy + 23), 4, (72, 72, 72), -1)
            # Different markings inside the template circle prevent eye swaps.
            mark_y = cy - 27 if side == -1 else cy + 27
            cv2.rectangle(self.face, (cx - 22, mark_y - 3),
                          (cx + 14, mark_y + 3), (65, 65, 65), -1)
        cv2.ellipse(self.face, (center[0], center[1] + 5), (15, 10),
                    0, 0, 360, (60, 60, 60), -1)
        cv2.line(self.face, (center[0] - 24, center[1] + 44),
                 (center[0] + 19, center[1] + 46), (65, 65, 65), 2)

    def eye_at(self, point, state):
        hx, hy = state[:2]
        for x, y in self.eyes:
            center = (x + hx, y + hy)
            if ((point[0] - center[0]) / 44) ** 2 + ((point[1] - center[1]) / 25) ** 2 <= 1:
                return center
        return None

    @staticmethod
    def gaze(point, center):
        dx, dy = point[0] - center[0], point[1] - center[1]
        norm = math.hypot(dx / 27, dy / 13)
        return dx / max(1, norm), dy / max(1, norm)

    def render(self, state, now):
        hx, hy, gx, gy, blink_until = state
        # Integer translation preserves pixel edges and feature sizes.
        frame = cv2.warpAffine(self.face, np.float32([[1, 0, round(hx)], [0, 1, round(hy)]]),
                               (self.width, self.height), flags=cv2.INTER_NEAREST,
                               borderValue=(145, 145, 145)) if hx or hy else self.face.copy()
        for x, y in self.eyes:
            cx, cy = round(x + hx), round(y + hy)
            if now < blink_until:
                cv2.ellipse(frame, (cx, cy), (44, 25), 0, 0, 360, (166, 166, 166), -1)
                cv2.line(frame, (cx - 40, cy), (cx + 40, cy), (65, 65, 65), 2)
            else:
                cv2.ellipse(frame, (round(cx + gx), round(cy + gy)), (11, 9),
                            10, 0, 360, (15, 15, 15), -1)
                cv2.circle(frame, (cx + 5, cy - 4), 2, (245, 245, 245), -1)
        return frame
