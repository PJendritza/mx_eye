"""Pupil/CR and template tracker extracted from Patrick's v13 prototype.

Detection, continuity gates, pair geometry and reacquisition retain v13 logic.
No GUI, disk I/O or networking is performed here. Coordinates are image pixels.
"""
import math
from collections import deque
import cv2
import numpy as np
from .config import PUPIL_METHODS, PupilMethod, PupilCoordinates, TrackingConfig, TrackingMode
from .pupil_methods import adaptive_mask, detect_edges

class TrackState:

    def __init__(self):
        self.history = deque(maxlen=8)
        self.seed = None
        self.lost = 0

    def clear(self):
        self.history.clear()
        self.seed = None
        self.lost = 0

    def last(self):
        return self.history[-1] if self.history else None

    def prediction(self):
        if self.history:
            last = self.history[-1]
            if self.lost == 0 and len(self.history) >= 2:
                prev = self.history[-2]
                df = max(1, last['frame'] - prev['frame'])
                vx = (last['x'] - prev['x']) / df
                vy = (last['y'] - prev['y']) / df
                return (last['x'] + vx, last['y'] + vy)
            return (last['x'], last['y'])
        if self.seed is not None:
            return self.seed
        return None

class Tracker:
    def __init__(self, config=None):
        self.config = (config or TrackingConfig()).model_copy(deep=True)
        self.roi = list(self.config.roi)
        self.current_frame = None
        self.frame_idx = 0
        self.pupil_state = TrackState()
        self.cr_state = TrackState()
        self.last_valid_pair_vector = None
        self.pair_lost_frames = 0
        self.last_pupil = self.last_cr = None
        self.template = None
        self.template_anchor = None
        self.template_last_center = None
        self.template_score = float('nan')
        self.template_corr_map = None
        self.template_corr_origin = None
        self.template_corr_peak = None
        self.pick_status = self.track_status = self.template_status = ''
        self.reject_reason = ''
        self._area_rejections = {}
        self._gray_cache = None
        self.pupil_evidence = None

    def process(self, frame, frame_id, advance=True, frame_delta=1):
        # The acquired frame remains untouched for recording. Use the same
        # operation offline with the multiplier stored beside each sample.
        self.current_frame = (frame if self.config.image_brightness == 1.0 else
                              cv2.convertScaleAbs(frame, alpha=self.config.image_brightness))
        self._gray_cache = None
        self.pupil_evidence = None
        self.reject_reason = ''
        self._area_rejections = {}
        self.frame_idx = frame_id
        self.clamp_roi()
        if advance:
            self.track_template()
        pupil, cr, _ = self.detect_pupil_cr(advance_state=advance, frame_delta=frame_delta)
        self.last_pupil, self.last_cr = pupil, cr
        pupil_only = self.config.tracking_mode is TrackingMode.PUPIL_ONLY
        valid = pupil is not None and (pupil_only or cr is not None)
        x = pupil['x'] - (0 if pupil_only else cr['x']) if valid else float('nan')
        y = pupil['y'] - (0 if pupil_only else cr['y']) if valid else float('nan')
        if valid and pupil_only and self.config.pupil_coordinates is PupilCoordinates.RELATIVE:
            x -= self.roi[0]
            y -= self.roi[1]
        return dict(x=x, y=y, valid=valid, pupil=pupil, cr=cr,
                    pupil_method=self.config.pupil_method,
                    roi=list(self.roi), template_score=self.template_score,
                    template_center=self.template_last_center,
                    template_anchor=self.template_anchor,
                    search_rect=self.fixed_search_rect(),
                    track_status=self.track_status, template_status=self.template_status,
                    reject_reason=self.reject_reason if not valid else '')

    def gray_region(self, x1, y1, x2, y2):
        """Convert only needed pixels, sharing the template search conversion with ROI."""
        if self.current_frame.ndim == 2:
            return self.current_frame[y1:y2, x1:x2]
        if self._gray_cache is not None:
            (cx1, cy1, cx2, cy2), gray = self._gray_cache
            if cx1 <= x1 and cy1 <= y1 and x2 <= cx2 and y2 <= cy2:
                return gray[y1-cy1:y2-cy1, x1-cx1:x2-cx1]
        gray = cv2.cvtColor(self.current_frame[y1:y2, x1:x2], cv2.COLOR_BGR2GRAY)
        self._gray_cache = ((x1, y1, x2, y2), gray)
        return gray


    def clamp_roi(self):
        if self.current_frame is None:
            return
        h0, w0 = self.current_frame.shape[:2]
        x, y, w, h = self.roi
        w = max(30, min(int(round(w)), w0))
        h = max(30, min(int(round(h)), h0))
        x = max(0, min(int(round(x)), w0 - w))
        y = max(0, min(int(round(y)), h0 - h))
        self.roi = [x, y, w, h]

    def pick_feature(self, kind, sx, sy):
        gray = cv2.cvtColor(self.current_frame, cv2.COLOR_BGR2GRAY)
        h, w = gray.shape
        cx = int(round(sx))
        cy = int(round(sy))
        r_outer = 14
        r_center = 3
        x1 = max(0, cx - r_outer)
        x2 = min(w, cx + r_outer + 1)
        y1 = max(0, cy - r_outer)
        y2 = min(h, cy + r_outer + 1)
        patch = gray[y1:y2, x1:x2]
        yy, xx = np.ogrid[y1:y2, x1:x2]
        rr2 = (xx - cx) ** 2 + (yy - cy) ** 2
        center_mask = rr2 <= r_center * r_center
        ring_mask = (rr2 >= 7 * 7) & (rr2 <= r_outer * r_outer)
        center_vals = patch[center_mask]
        ring_vals = patch[ring_mask]
        if center_vals.size == 0 or ring_vals.size == 0:
            self.pick_status = 'Could not estimate threshold there.'
            return
        center_med = float(np.median(center_vals))
        ring_med = float(np.median(ring_vals))
        if kind == 'pupil':
            if center_med < ring_med:
                threshold = 0.5 * (center_med + ring_med)
            else:
                threshold = float(np.percentile(patch, 20))
            self.config.pupil_thr = int(round(np.clip(threshold, 0, 255)))
            self.pupil_state.clear()
            self.pupil_state.seed = (float(cx), float(cy))
            self.pair_lost_frames = 0
            self.pick_status = f"Pupil seeded at ({cx},{cy})"
            if self.config.pupil_method is PupilMethod.THRESHOLD:
                self.pick_status += f"; threshold ≈ {self.config.pupil_thr:.0f}"
        else:
            if center_med > ring_med:
                threshold = 0.5 * (center_med + ring_med)
            else:
                threshold = float(np.percentile(patch, 95))
            self.config.cr_thr = int(round(np.clip(threshold, 0, 255)))
            self.cr_state.clear()
            self.cr_state.seed = (float(cx), float(cy))
            self.pair_lost_frames = 0
            self.pick_status = f"CR seeded at ({cx},{cy}); threshold ≈ {self.config.cr_thr:.0f}"
        self.last_valid_pair_vector = None

    def clear_feature_history(self):
        self.pupil_state.clear()
        self.cr_state.clear()
        self.last_valid_pair_vector = None
        self.pair_lost_frames = 0
        self.last_pupil = None
        self.last_cr = None
        self.track_status = 'History cleared'

    def set_template(self, sx, sy):
        gray = cv2.cvtColor(self.current_frame, cv2.COLOR_BGR2GRAY)
        r = max(6, int(round(self.config.template_radius)))
        cx = int(round(sx))
        cy = int(round(sy))
        if cx - r < 0 or cy - r < 0 or cx + r >= gray.shape[1] or (cy + r >= gray.shape[0]):
            return
        patch = gray[cy - r:cy + r + 1, cx - r:cx + r + 1].copy()
        yy, xx = np.ogrid[-r:r + 1, -r:r + 1]
        circle = xx * xx + yy * yy <= r * r
        mean_inside = float(patch[circle].mean())
        patch[~circle] = int(round(mean_inside))
        self.template = patch
        self.template_corr_map = None
        self.template_corr_origin = None
        self.template_corr_peak = None
        if self.template_anchor is None:
            self.template_anchor = (cx, cy)
        self.template_last_center = (cx, cy)
        self.template_score = 1.0
        self.config.template_tracking = True
        self.template_status = 'Tracking 1.00'

    def fixed_search_rect(self):
        if self.template_anchor is None or self.current_frame is None:
            return None
        size = max(100, int(round(self.config.template_search_size)))
        half = size // 2
        h, w = self.current_frame.shape[:2]
        cx, cy = self.template_anchor
        cx = int(round(cx))
        cy = int(round(cy))
        x1 = int(max(0, cx - half))
        y1 = int(max(0, cy - half))
        x2 = int(min(w, cx + half))
        y2 = int(min(h, cy + half))
        return (x1, y1, x2, y2)

    def track_template(self):
        if not self.config.template_tracking or self.template is None or self.template_last_center is None or (self.current_frame is None):
            return
        rect = self.fixed_search_rect()
        if rect is None:
            return
        x1, y1, x2, y2 = rect
        search = self.gray_region(x1, y1, x2, y2)
        th, tw = self.template.shape[:2]
        if search.shape[0] < th or search.shape[1] < tw:
            self.template_score = np.nan
            self.template_corr_map = None
            self.template_corr_origin = None
            self.template_corr_peak = None
            self.template_status = 'Search too small'
            return
        result = cv2.matchTemplate(search, self.template, cv2.TM_CCOEFF_NORMED)
        _, max_val, _, max_loc = cv2.minMaxLoc(result)
        self.template_score = float(max_val)
        center_x0 = int(x1 + tw // 2)
        center_y0 = int(y1 + th // 2)
        self.template_corr_map = result
        self.template_corr_origin = (center_x0, center_y0)
        self.template_corr_peak = (center_x0 + int(max_loc[0]), center_y0 + int(max_loc[1]))
        min_corr = float(self.config.template_min_corr)
        if max_val < min_corr:
            self.template_status = f'LOST {max_val:.2f} < {min_corr:.2f}'
            return
        new_cx = x1 + max_loc[0] + tw // 2
        new_cy = y1 + max_loc[1] + th // 2
        old_cx, old_cy = self.template_last_center
        dx = new_cx - old_cx
        dy = new_cy - old_cy
        self.roi[0] += dx
        self.roi[1] += dy
        self.template_last_center = (new_cx, new_cy)
        self.clamp_roi()
        self.template_status = f'Track {max_val:.2f} ≥ {min_corr:.2f}'

    def clear_template(self):
        self.template = None
        self.template_anchor = None
        self.template_last_center = None
        self.template_score = np.nan
        self.template_corr_map = None
        self.template_corr_origin = None
        self.template_corr_peak = None
        self.config.template_tracking = False
        self.template_status = 'No template'

    def component_candidates(self, mask, min_area, max_area, roi_x, roi_y, kind):
        n, labels, stats, cents = cv2.connectedComponentsWithStats(mask, connectivity=8)
        candidates = []
        # Filter labels in compiled NumPy before touching Python candidate objects.
        areas = stats[1:, cv2.CC_STAT_AREA]
        selected = np.flatnonzero((areas >= min_area) & (areas <= max_area)) + 1
        if not selected.size:
            if areas.size:
                name = 'Pupil' if kind == 'pupil' else 'CR'
                if np.all(areas < min_area):
                    self._area_rejections[kind] = f'{name} too small (largest {int(areas.max())} px²; minimum {min_area} px²)'
                elif np.all(areas > max_area):
                    self._area_rejections[kind] = f'{name} too large (smallest {int(areas.min())} px²; maximum {max_area} px²)'
                else:
                    self._area_rejections[kind] = f'No {name.lower()} region within {min_area}–{max_area} px²'
            else:
                self._area_rejections[kind] = f'No {kind.upper()} pixels at current threshold'
        for i in selected:
            area = int(stats[i, cv2.CC_STAT_AREA])
            if area < min_area or area > max_area:
                continue
            cx = float(cents[i, 0])
            cy = float(cents[i, 1])
            cand = {'label': int(i), 'area': area, 'local_x': cx, 'local_y': cy, 'x': roi_x + cx, 'y': roi_y + cy, 'ellipse': None, 'aspect': 1.0, 'fill': 1.0}
            if kind == 'pupil':
                left, top, width, height = map(int, stats[i, :4])
                component = cv2.compare(labels[top:top+height, left:left+width], int(i), cv2.CMP_EQ)
                contours, _ = cv2.findContours(component, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_NONE,
                                               offset=(left, top))
                if contours:
                    contour = max(contours, key=cv2.contourArea)
                    if len(contour) >= 5:
                        ellipse = cv2.fitEllipse(contour)
                        ecx, ecy = ellipse[0]
                        a, b = ellipse[1]
                        cand['local_x'] = float(ecx)
                        cand['local_y'] = float(ecy)
                        cand['x'] = roi_x + float(ecx)
                        cand['y'] = roi_y + float(ecy)
                        cand['ellipse'] = ellipse
                        small = max(1e-06, min(a, b))
                        large = max(a, b)
                        cand['aspect'] = large / small
                        ellipse_area = math.pi * (a / 2) * (b / 2)
                        if ellipse_area > 0:
                            cand['fill'] = area / ellipse_area
            candidates.append(cand)
        return candidates

    def candidate_score(self, cand, state, base_gate, kind, reacquire=False):
        if reacquire:
            score = 0.0
        else:
            pred = state.prediction()
            if pred is not None:
                expansion = min(3.0, 1.0 + 0.35 * state.lost)
                gate = base_gate * expansion
                dist = math.hypot(cand['x'] - pred[0], cand['y'] - pred[1])
                if dist > gate:
                    return None
                score = dist / max(gate, 1e-06)
            else:
                score = 0.5
            last = state.last()
            if last is not None and last.get('area', None):
                ratio = cand['area'] / max(last['area'], 1)
                if ratio < 0.2 or ratio > 5.0:
                    return None
                score += 0.3 * abs(math.log(max(ratio, 1e-06)))
        if kind == 'pupil':
            aspect = max(1.0, cand.get('aspect', 1.0))
            score += 0.12 * max(0.0, aspect - 1.6)
            fill = cand.get('fill', 1.0)
            if np.isfinite(fill):
                score += 0.08 * abs(fill - 1.0)
        return score

    def missing_reason(self, kind, candidates, scored, gate, reacquire):
        name = 'Pupil' if kind == 'pupil' else 'CR'
        if not candidates:
            return self._area_rejections.get(kind, f'No {name.lower()} candidate passed detection and area checks')
        if not scored:
            if reacquire:
                return f'No {name.lower()} candidate passed reacquisition checks'
            state = self.pupil_state if kind == 'pupil' else self.cr_state
            prediction = state.prediction()
            if prediction is not None:
                nearest = min(math.hypot(c['x']-prediction[0], c['y']-prediction[1]) for c in candidates)
                limit = gate * min(3.0, 1.0 + 0.35 * state.lost)
                if nearest > limit:
                    return f'{name} moved {nearest:.1f} px; gate {limit:.1f} px'
            return f'{name} area changed too much from previous frame'
        return ''

    def choose_candidates(self, pupil_candidates, cr_candidates, reacquire=False):
        p_gate = float(self.config.pupil_gate)
        c_gate = float(self.config.cr_gate)
        max_pair = float(self.config.max_pair_dist)
        max_vec_change = float(self.config.max_pair_vec_change)
        p_scored = []
        for cand in pupil_candidates:
            score = self.candidate_score(cand, self.pupil_state, p_gate, 'pupil', reacquire=reacquire)
            if score is not None:
                p_scored.append((score, cand))
        c_scored = []
        for cand in cr_candidates:
            score = self.candidate_score(cand, self.cr_state, c_gate, 'cr', reacquire=reacquire)
            if score is not None:
                c_scored.append((score, cand))
        p_scored.sort(key=lambda z: z[0])
        c_scored.sort(key=lambda z: z[0])
        if not p_scored:
            self.reject_reason = self.missing_reason('pupil', pupil_candidates, p_scored, p_gate, reacquire)
        elif not c_scored:
            self.reject_reason = self.missing_reason('cr', cr_candidates, c_scored, c_gate, reacquire)
        best_pair = None
        nearest_pair = float('inf')
        nearest_vector_change = float('inf')
        for ps, pc in p_scored:
            if best_pair is not None and ps >= best_pair[0]:
                break
            for cs, cc in c_scored:
                # Remaining distance/vector penalties are nonnegative.
                if best_pair is not None and ps + cs >= best_pair[0]:
                    break
                dx = cc['x'] - pc['x']
                dy = cc['y'] - pc['y']
                pair_dist = math.hypot(dx, dy)
                nearest_pair = min(nearest_pair, pair_dist)
                if pair_dist > max_pair:
                    continue
                pair_score = ps + cs + 0.35 * (pair_dist / max(max_pair, 1e-06))
                if not reacquire and self.last_valid_pair_vector is not None:
                    pvx, pvy = self.last_valid_pair_vector
                    vec_change = math.hypot(dx - pvx, dy - pvy)
                    nearest_vector_change = min(nearest_vector_change, vec_change)
                    if vec_change > max_vec_change:
                        continue
                    pair_score += 0.45 * (vec_change / max(max_vec_change, 1e-06))
                if best_pair is None or pair_score < best_pair[0]:
                    best_pair = (pair_score, pc, cc, (dx, dy))
        if best_pair is not None:
            self.reject_reason = ''
            _, pupil, cr, pair_vec = best_pair
            return (pupil, cr, pair_vec, 'REACQUIRED' if reacquire else 'PAIR')
        if p_scored and c_scored:
            if nearest_pair > max_pair:
                self.reject_reason = f'Pupil–CR distance {nearest_pair:.1f} px exceeds {max_pair:g} px'
            elif nearest_vector_change > max_vec_change and np.isfinite(nearest_vector_change):
                self.reject_reason = f'Pupil–CR vector changed {nearest_vector_change:.1f} px; limit {max_vec_change:g} px'
            else:
                self.reject_reason = 'No pupil–CR pair passed tracking checks'
        if reacquire:
            return (None, None, None, 'REACQUIRE')
        best_p = p_scored[0] if p_scored else None
        best_c = c_scored[0] if c_scored else None
        if best_p is not None and best_c is not None:
            if best_p[0] <= best_c[0]:
                return (best_p[1], None, None, 'PUPIL ONLY')
            return (None, best_c[1], None, 'CR ONLY')
        if best_p is not None:
            return (best_p[1], None, None, 'PUPIL ONLY')
        if best_c is not None:
            return (None, best_c[1], None, 'CR ONLY')
        return (None, None, None, 'LOST')

    def update_track_state(self, state, detection, frame_delta=1):
        frame_delta = max(1, int(frame_delta))
        if detection is None:
            state.lost += frame_delta
            return
        state.history.append({'frame': self.frame_idx, 'x': float(detection['x']), 'y': float(detection['y']), 'area': int(detection['area'])})
        state.seed = None
        state.lost = 0

    def detect_pupil_cr(self, advance_state=True, frame_delta=1):
        if self.current_frame is None:
            self.reject_reason = 'No image available'
            return (None, None, None)
        x, y, w, h = [int(round(v)) for v in self.roi]
        crop = self.current_frame[y:y + h, x:x + w]
        if crop.size == 0:
            self.reject_reason = 'Tracking ROI is outside the image'
            return (None, None, None)
        gray = self.gray_region(x, y, x+w, y+h)
        pthr = int(round(self.config.pupil_thr))
        cthr = int(round(self.config.cr_thr))
        pmin = max(0, int(round(self.config.pupil_min)))
        pmax = max(pmin, int(round(self.config.pupil_max)))
        cmin = max(0, int(round(self.config.cr_min)))
        cmax = max(cmin, int(round(self.config.cr_max)))
        method = self.config.pupil_method
        if method in (PupilMethod.THRESHOLD, PupilMethod.ADAPTIVE):
            pupil_mask = cv2.compare(gray, pthr, cv2.CMP_LT) if method is PupilMethod.THRESHOLD else adaptive_mask(gray, self.config.model_dump(mode="json"))
            self.pupil_evidence = pupil_mask
            pupil_candidates = self.component_candidates(pupil_mask, pmin, pmax, x, y, 'pupil')
        else:
            prediction = self.pupil_state.prediction()
            seed = (prediction[0]-x, prediction[1]-y) if prediction is not None else None
            pupil_candidates, self.pupil_evidence = detect_edges(gray, self.config.model_dump(mode="json"), (x,y), seed)
        pupil_only = self.config.tracking_mode is TrackingMode.PUPIL_ONLY
        if pupil_only:
            cr_mask = None
        else:
            cr_mask = cv2.compare(gray, cthr, cv2.CMP_GT)
        cr_candidates = []
        if not pupil_only:
            cr_candidates = self.component_candidates(cr_mask, cmin, cmax, x, y, 'cr')
        reacquire_n = max(1, int(round(self.config.reacquire_after_frames)))
        reacquire = self.pair_lost_frames >= reacquire_n
        if pupil_only:
            p_scored = []
            for cand in pupil_candidates:
                score = self.candidate_score(cand, self.pupil_state, float(self.config.pupil_gate), 'pupil', reacquire=reacquire)
                if score is not None:
                    p_scored.append((score, cand))
            p_scored.sort(key=lambda z: z[0])
            pupil = p_scored[0][1] if p_scored else None
            self.reject_reason = '' if pupil is not None else self.missing_reason(
                'pupil', pupil_candidates, p_scored, float(self.config.pupil_gate), reacquire)
            cr = None
            pair_vec = None
            if pupil is not None:
                mode = 'REACQUIRED' if reacquire else 'PUPIL'
            else:
                mode = 'REACQUIRE' if reacquire else 'LOST'
            if advance_state:
                frame_delta = max(1, int(frame_delta))
                if pupil is not None:
                    if reacquire:
                        self.pupil_state.clear()
                    self.update_track_state(self.pupil_state, pupil, frame_delta=frame_delta)
                    self.pair_lost_frames = 0
                else:
                    self.update_track_state(self.pupil_state, None, frame_delta=frame_delta)
                    self.pair_lost_frames += frame_delta
            self.track_status = f'{mode}  lost:{self.pair_lost_frames}/{reacquire_n}  P:{self.pupil_state.lost}'
        else:
            pupil, cr, pair_vec, mode = self.choose_candidates(pupil_candidates, cr_candidates, reacquire=reacquire)
            if advance_state:
                frame_delta = max(1, int(frame_delta))
                if pupil is not None and cr is not None and (pair_vec is not None):
                    if reacquire:
                        self.pupil_state.clear()
                        self.cr_state.clear()
                        self.last_valid_pair_vector = None
                    self.update_track_state(self.pupil_state, pupil, frame_delta=frame_delta)
                    self.update_track_state(self.cr_state, cr, frame_delta=frame_delta)
                    self.last_valid_pair_vector = pair_vec
                    self.pair_lost_frames = 0
                else:
                    self.update_track_state(self.pupil_state, pupil, frame_delta=frame_delta)
                    self.update_track_state(self.cr_state, cr, frame_delta=frame_delta)
                    self.pair_lost_frames += frame_delta
            status_mode = mode
            if self.pair_lost_frames >= reacquire_n and mode != 'REACQUIRED':
                status_mode = 'REACQUIRE'
            self.track_status = f'{status_mode}  pair lost:{self.pair_lost_frames}/{reacquire_n}  P:{self.pupil_state.lost} CR:{self.cr_state.lost}'
        self.track_status = PUPIL_METHODS[method]+' · '+self.track_status
        return (pupil, cr, None)
