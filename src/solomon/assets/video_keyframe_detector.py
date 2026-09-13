"""
视频关键帧智能提取器 v5.0 - MAR 状态机 + 三模态干扰检测 + 字幕区自学习 + MOG2
Phase 5.0: 底部字幕区持续学习 + 冷启动软权重 + MOG2弹框检测 + 前置掩膜pHash

核心改进（相比 v4.1）：
1. SubtitleZoneDetector — 底部20%区域环形buffer持续学习字幕位置
2. 冷启动软权重 — 环形buffer未满时底部区域MAR值×0.5，不完全置零
3. MOG2背景减除 — 替代median，检测瞬态弹框（录屏弹窗等），单帧~10ms
4. Region-Masked pHash — 输出前涂黑干扰区再算哈希，阈值从8降到5
5. 掩膜合并 — 三模态+字幕学习+MOG2合并成一张复合掩膜
6. 保留v4.1全部功能：Pulse/HeatMap/Avatar/Flip Freeze/双哈希/自适应阈值

用法：python3 video_keyframe_detector.py <video_path> [output_dir]
"""

import cv2
import numpy as np
import time
import uuid
import os
import sys
from collections import deque

try:
    from PIL import Image
    import imagehash
except ImportError:
    imagehash = None
    print("[Warning] pip install Pillow imagehash")


class SubtitleZoneDetector:
    """
    底部字幕区自学习检测器
    
    策略：监控底部20%区域的像素变化频率
    - 高频变化的行 → 字幕行（加入掩膜）
    - 低频变化的行 → 正常内容（不掩膜）
    
    冷启动：环形buffer未满时，底部20%区域MAR值打折（×0.5）
    """
    
    def __init__(self, frame_width=480, frame_height=270):
        self.fw = frame_width
        self.fh = frame_height
        
        # 只监控底部20%区域
        self.bottom_top = int(frame_height * 0.80)  # 第216行（480x270中）
        self.bottom_h = frame_height - self.bottom_top  # 54行
        
        # 环形buffer：20帧底部区域灰度图（降采样到120x27，极轻量）
        self.buffer_size = 20
        self.ring_buffer = deque(maxlen=self.buffer_size)
        
        # 字幕区掩膜（与frame同size）
        self.subtitle_mask = np.zeros((frame_height, frame_width), dtype=np.uint8)
        self.mask_ready = False
        
        # 更新频率：每6帧（~2.4秒）重新算一次
        self.update_interval = 6
        self.frame_counter = 0
        
        # 冷启动软权重
        self.soft_weight = 0.5
        self.is_warm = False
        
        # 字幕行判定阈值：方差 > 全局中位数的3倍
        self.variance_threshold_factor = 3.0
    
    def update(self, gray_frame):
        """
        每帧调用：更新环形buffer，定期更新掩膜
        输入：480x270灰度帧
        """
        # 提取底部区域并降采样
        bottom_region = gray_frame[self.bottom_top:, :]
        bottom_small = cv2.resize(bottom_region, (120, self.bottom_h), 
                                   interpolation=cv2.INTER_AREA)
        self.ring_buffer.append(bottom_small)
        
        self.frame_counter += 1
        
        # 冷启动检查
        if not self.is_warm:
            if len(self.ring_buffer) >= self.buffer_size:
                self.is_warm = True
            else:
                return  # buffer未满，不更新掩膜
        
        # 定期更新掩膜
        if self.frame_counter % self.update_interval == 0:
            self._compute_mask()
    
    def _compute_mask(self):
        """从环形buffer计算字幕行掩膜"""
        if len(self.ring_buffer) < 5:
            return
        
        # 沿时间轴算方差
        stack = np.stack(list(self.ring_buffer), axis=0).astype(np.float32)
        variance = np.var(stack, axis=0)  # shape: (bottom_h, 120)
        
        # 水平投影：每行的平均方差
        row_variance = variance.mean(axis=1)  # shape: (bottom_h,)
        
        # 阈值：全局中位数的N倍
        median_var = np.median(row_variance)
        threshold = median_var * self.variance_threshold_factor
        
        # 标记高方差行（字幕行）
        subtitle_rows = row_variance > threshold
        
        # 向上膨胀2行（防止字间距导致掩膜断裂）
        kernel = np.ones((3, 1), np.uint8)
        subtitle_rows_uint8 = subtitle_rows.astype(np.uint8) * 255
        subtitle_rows_dilated = cv2.dilate(
            subtitle_rows_uint8.reshape(-1, 1), kernel, iterations=1
        ).flatten() > 0
        
        # 写入掩膜（底部区域对应的原始坐标）
        self.subtitle_mask[:] = 0
        for i, is_sub in enumerate(subtitle_rows_dilated):
            if is_sub:
                row_y = self.bottom_top + i
                if row_y < self.fh:
                    self.subtitle_mask[row_y, :] = 255
        
        self.mask_ready = True
    
    def get_soft_weight_mask(self):
        """
        冷启动期间：返回底部20%区域的软权重掩膜
        热启动后：返回精确的字幕行掩膜
        """
        if self.mask_ready:
            return self.subtitle_mask
        
        # 冷启动：底部20%区域全部标记为"软权重"
        mask = np.zeros((self.fh, self.fw), dtype=np.uint8)
        mask[self.bottom_top:, :] = 128  # 128 = 软权重标记
        return mask


class SmartKeyframeExtractor:
    def __init__(self, resize=(480, 270), fps_sample=2.5):
        self.resize = resize
        self.fps_sample = fps_sample

        # 帧缓冲
        self.frame_buffer = deque(maxlen=3)
        self.gray_buffer = deque(maxlen=2)

        # 状态机
        self.state = 'STABLE'
        self.stable_counter = 0
        self.motion_duration = 0

        # last_stable_frame
        self.last_stable_frame = None

        # 动态掩膜
        self.dynamic_mask = np.zeros((resize[1], resize[0]), dtype=np.uint8)

        # 双哈希去重（v5.0：阈值从8降到5）
        self.last_output_phash = None
        self.last_output_dhash = None
        self.phash_history = deque(maxlen=5)  # 最近5帧的hash

        # 阈值
        self.TH_FLIP = 0.35
        self.TH_NOISE = 0.02
        self.WAIT_ANNOTATE = max(2, int(2.4 * fps_sample))
        self.WAIT_BASE = max(1, int(0.8 * fps_sample))
        self.mar_history = deque(maxlen=100)

        # === Phase 2.1: 三模态干扰检测 ===
        # Pulse Detector
        self.grid_rows = 17
        self.grid_cols = 30
        self.grid_size = (self.grid_cols, self.grid_rows)
        self.history_frames = 25
        self.pulse_threshold = 3
        self.grid_history = deque(maxlen=self.history_frames)

        # HeatMap
        self.heat_map = np.zeros((self.grid_rows, self.grid_cols), dtype=np.float32)
        self.heat_threshold = 7.5

        # Avatar Mask
        self.avatar_mask = np.zeros((self.grid_rows, self.grid_cols), dtype=np.uint8)
        self.avatar_presence = {}

        # Cooldown
        self.cooldown_map = np.zeros((self.grid_rows, self.grid_cols), dtype=np.uint8)
        self.max_cooldown = 12

        # Flip Freeze
        self.freeze_mask_update = False

        # 冷启动
        self.warmup_frames = 0

        # === Phase 5.0: 字幕区自学习 + MOG2 ===
        self.subtitle_detector = SubtitleZoneDetector(resize[0], resize[1])
        
        # MOG2背景减除器（检测瞬态弹框）
        self.mog2 = cv2.createBackgroundSubtractorMOG2(
            history=50,         # 约20秒的历史@2.5fps
            varThreshold=50,    # 较高阈值，减少误报
            detectShadows=False # 不检测阴影，节省计算
        )
        self.mog2_learning_rate = 1.0 / 50  # 与history匹配

    @staticmethod
    def _compute_phash(frame, hash_size=8):
        """感知哈希：DCT低频分量"""
        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        resized = cv2.resize(gray, (hash_size * 4, hash_size * 4))
        dct = cv2.dct(resized.astype(np.float32))
        dct_low = dct[:hash_size, :hash_size]
        avg = dct_low.mean()
        return (dct_low > avg).flatten()

    @staticmethod
    def _compute_dhash(frame):
        pil = Image.fromarray(cv2.cvtColor(frame, cv2.COLOR_BGR2RGB))
        return imagehash.dhash(pil)

    def _get_diff_mar(self, curr_gray, prev_gray):
        diff = cv2.absdiff(curr_gray, prev_gray)
        _, thresh = cv2.threshold(diff, 25, 255, cv2.THRESH_BINARY)
        
        # 合并动态掩膜（现有三模态）
        if np.any(self.dynamic_mask):
            thresh = cv2.bitwise_and(thresh, thresh, mask=cv2.bitwise_not(self.dynamic_mask))
        
        # === Phase 5.0: 字幕区掩膜 ===
        sub_mask = self.subtitle_detector.get_soft_weight_mask()
        if self.subtitle_detector.is_warm and self.subtitle_detector.mask_ready:
            # 热启动：精确掩膜，完全屏蔽字幕行
            thresh = cv2.bitwise_and(thresh, thresh, mask=cv2.bitwise_not(sub_mask))
        elif np.any(sub_mask > 0):
            # 冷启动：软权重，底部区域的MAR值打折
            soft_zone = (sub_mask == 128).astype(np.uint8) * 255
            # 先正常算
            mar_raw = np.count_nonzero(thresh) / (self.resize[0] * self.resize[1])
            # 软权重区域单独算
            soft_diff = cv2.bitwise_and(thresh, thresh, mask=soft_zone)
            soft_mar = np.count_nonzero(soft_diff) / (self.resize[0] * self.resize[1])
            # 最终MAR = 非软区 + 软区×0.5
            non_soft_mar = mar_raw - soft_mar
            mar_weighted = non_soft_mar + soft_mar * self.subtitle_detector.soft_weight
            
            # 对thresh本身也做软化处理（用于后续的可视化和调试）
            kernel = np.ones((3, 3), np.uint8)
            cleaned = cv2.morphologyEx(thresh, cv2.MORPH_OPEN, kernel)
            return mar_weighted, cleaned
        
        kernel = np.ones((3, 3), np.uint8)
        cleaned = cv2.morphologyEx(thresh, cv2.MORPH_OPEN, kernel)
        mar = np.count_nonzero(cleaned) / (self.resize[0] * self.resize[1])
        return mar, cleaned

    def _update_adaptive_noise(self):
        if len(self.mar_history) < 20:
            self.TH_NOISE = 0.02
        else:
            self.TH_NOISE = float(np.clip(np.median(self.mar_history) * 3, 0.01, 0.04))

    def _commit_stable_frame(self, frame):
        self.last_stable_frame = frame.copy()

    def _is_duplicate(self, frame):
        """
        v5.0: Region-Masked pHash去重
        先涂黑干扰区，再算哈希，阈值降到5
        """
        if imagehash is None:
            return False
        
        # 涂黑干扰区：用dynamic_mask + subtitle_mask把干扰区域置零
        # 注意：mask是480x270，原始帧可能更大，需要resize mask
        h, w = frame.shape[:2]
        masked_frame = frame.copy()
        combined_mask = self.dynamic_mask.copy()
        
        # 叠加字幕掩膜
        if self.subtitle_detector.mask_ready:
            combined_mask = cv2.bitwise_or(combined_mask, self.subtitle_detector.subtitle_mask)
        
        # 在干扰区域涂黑（resize mask到帧尺寸）
        if np.any(combined_mask):
            mask_full = cv2.resize(combined_mask, (w, h), interpolation=cv2.INTER_NEAREST)
            masked_frame[mask_full > 0] = [0, 0, 0]
        
        # 用涂黑后的帧算pHash
        ph = self._compute_phash(masked_frame)
        
        # dHash用原始帧（保留结构信息）
        dh = self._compute_dhash(frame)
        
        # 和最近5帧比较（numpy布尔数组用sum计算汉明距离）
        for prev_ph, prev_dh in self.phash_history:
            ph_dist = np.sum(ph != prev_ph)
            dh_dist = dh - prev_dh  # imagehash.ImageHash支持减法
            if ph_dist < 5 and dh_dist < 5:
                return True
        
        self.phash_history.append((ph, dh))
        return False

    # === Phase 2.1: 三模态干扰检测（不变） ===

    def _update_dynamic_mask(self, cleaned_diff):
        """三模态干扰检测：Pulse + HeatMap + Avatar"""
        if self.freeze_mask_update:
            return

        grid_diff = cv2.resize(cleaned_diff, self.grid_size, interpolation=cv2.INTER_AREA)
        curr_motion = (grid_diff > 10).astype(np.uint8)
        self.grid_history.append(curr_motion)

        self.warmup_frames += 1
        if self.warmup_frames < self.history_frames:
            return

        history_arr = np.array(self.grid_history)
        pulses = np.sum((history_arr[1:] - history_arr[:-1]) == 1, axis=0)

        contours, _ = cv2.findContours(
            (curr_motion * 255).astype(np.uint8), cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        large_area = sum(cv2.contourArea(c) for c in contours if cv2.contourArea(c) > 50)
        active_ratio = large_area / (self.grid_rows * self.grid_cols)
        if active_ratio > 0.4:
            return

        heat_diff = cv2.resize(cleaned_diff, self.grid_size, interpolation=cv2.INTER_AREA)
        self.heat_map = self.heat_map * 0.95 + (heat_diff > 10).astype(np.float32)

        self._update_avatar_mask(curr_motion)

        mask_pulse = (pulses >= self.pulse_threshold).astype(np.uint8) * 255
        mask_heat = (self.heat_map > self.heat_threshold).astype(np.uint8) * 255
        combined = mask_pulse | mask_heat | self.avatar_mask

        kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (3, 3))
        combined = cv2.morphologyEx(combined, cv2.MORPH_CLOSE, kernel)

        self.cooldown_map[combined > 0] = self.max_cooldown
        self.cooldown_map = np.maximum(self.cooldown_map - 1, 0)

        active = (self.cooldown_map > 0).astype(np.uint8) * 255
        if np.mean(active > 0) > 0.5:
            active = np.zeros_like(active)

        self.dynamic_mask = cv2.resize(active, self.resize, interpolation=cv2.INTER_NEAREST)

    def _update_avatar_mask(self, curr_motion):
        num_labels, labels, stats, _ = cv2.connectedComponentsWithStats(
            curr_motion * 255, connectivity=8)
        total_grid_area = self.grid_rows * self.grid_cols

        new_mask = np.zeros((self.grid_rows, self.grid_cols), dtype=np.uint8)
        for i in range(1, num_labels):
            area = stats[i, cv2.CC_STAT_AREA]
            if 20 < area < total_grid_area * 0.15:
                grid_mask = (labels == i).astype(np.uint8)
                key = i
                if key not in self.avatar_presence:
                    self.avatar_presence[key] = 0
                self.avatar_presence[key] = min(self.avatar_presence[key] + 1, self.history_frames)

                if self.avatar_presence[key] > self.history_frames * 0.7:
                    new_mask[labels == i] = 255

        for key in list(self.avatar_presence.keys()):
            found = False
            for i in range(1, num_labels):
                if stats[i, cv2.CC_STAT_AREA] > 20 and np.any(labels == i):
                    if key == i:
                        found = True
                        break
            if not found:
                self.avatar_presence[key] = max(0, self.avatar_presence[key] - 1)

        self.avatar_mask = new_mask

    def _check_scrolling(self, curr_gray, prev_gray):
        shift, response = cv2.phaseCorrelate(np.float32(prev_gray), np.float32(curr_gray))
        return abs(shift[1]) > 3.0 and response > 0.1

    def process_frame(self, frame, timestamp_ms):
        small = cv2.resize(frame, self.resize)
        gray = cv2.cvtColor(small, cv2.COLOR_BGR2GRAY)
        gray = cv2.GaussianBlur(gray, (5, 5), 0)

        self.frame_buffer.append(frame)
        self.gray_buffer.append(gray)
        if len(self.gray_buffer) < 2:
            return None, None

        curr_g = self.gray_buffer[-1]
        prev_g = self.gray_buffer[-2]

        # === Phase 5.0: 字幕区持续学习（每帧更新） ===
        self.subtitle_detector.update(curr_g)

        # === Phase 5.0: MOG2背景减除（检测瞬态弹框） ===
        mog2_mask = self.mog2.apply(curr_g, learningRate=self.mog2_learning_rate)
        # 过滤：太小(<1%)或太大(>30%)的前景忽略
        total_pixels = self.resize[0] * self.resize[1]
        contours_mog2, _ = cv2.findContours(
            mog2_mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        for cnt in contours_mog2:
            area = cv2.contourArea(cnt)
            ratio = area / total_pixels
            if 0.01 < ratio < 0.30:
                # 中等面积前景→可能是弹框，加入cooldown
                x, y, w, h = cv2.boundingRect(cnt)
                # 映射到grid坐标
                gx1 = int(x / self.resize[0] * self.grid_cols)
                gy1 = int(y / self.resize[1] * self.grid_rows)
                gx2 = min(int((x + w) / self.resize[0] * self.grid_cols), self.grid_cols - 1)
                gy2 = min(int((y + h) / self.resize[1] * self.grid_rows), self.grid_rows - 1)
                self.cooldown_map[gy1:gy2+1, gx1:gx2+1] = self.max_cooldown

        mar = 0.0

        # === 翻页冻结 ===
        if self.state == 'FLIPPING':
            self.freeze_mask_update = True
        else:
            self.freeze_mask_update = False
            mar_raw, cleaned = self._get_diff_mar(curr_g, prev_g)
            self._update_dynamic_mask(cleaned)
            if np.any(self.dynamic_mask):
                _, cleaned_masked = self._get_diff_mar(curr_g, prev_g)
                mar = np.count_nonzero(cleaned_masked) / (self.resize[0] * self.resize[1])
            else:
                mar = mar_raw

        if self.state == 'FLIPPING':
            mar = 0

        self.mar_history.append(mar if self.state != 'FLIPPING' else 0)
        self._update_adaptive_noise()

        # 翻页双阈值判定
        is_flip = False
        if self.state != 'FLIPPING':
            if mar > 0.5:
                is_flip = True
            elif mar > self.TH_FLIP and len(self.mar_history) >= 2:
                recent = list(self.mar_history)[-2:]
                if all(r > self.TH_FLIP for r in recent):
                    is_flip = True

        is_scrolling = is_flip and self._check_scrolling(curr_g, prev_g)

        output_frame = None

        if is_scrolling:
            self.state = 'SCROLLING'
            self.stable_counter = 0

        elif is_flip:
            if self.state in ['STABLE', 'ANNOTATING']:
                if self.last_stable_frame is not None:
                    output_frame = self.last_stable_frame
            self.state = 'FLIPPING'
            self.stable_counter = 0

        elif mar > self.TH_NOISE:
            if self.state == 'STABLE':
                self.state = 'ANNOTATING'
                self.motion_duration = 0
            self.motion_duration += 1
            self.stable_counter = 0
            force_frames = int(4.0 * self.fps_sample)
            if self.state == 'ANNOTATING' and self.motion_duration > force_frames:
                output_frame = self.frame_buffer[-1]
                self.motion_duration = 0

        else:
            self.stable_counter += 1
            self.motion_duration = 0
            wait = self.WAIT_ANNOTATE if self.state == 'ANNOTATING' else self.WAIT_BASE
            if self.state != 'STABLE' and self.stable_counter >= wait:
                output_frame = self.frame_buffer[-1]
                self.state = 'STABLE'

        if output_frame is not None:
            self._commit_stable_frame(output_frame)
            if not self._is_duplicate(output_frame):
                return output_frame, timestamp_ms
            return None, None

        return None, None


# === 兼容层（不变） ===

def frame_sampler(video_path, sample_fps=2.5):
    cap = cv2.VideoCapture(video_path)
    if not cap.isOpened():
        print(f"[Error] 无法打开: {video_path}")
        return
    native_fps = cap.get(cv2.CAP_PROP_FPS)
    if native_fps <= 0: native_fps = 30.0
    interval = max(1, int(native_fps / sample_fps))
    frame_idx = 0
    while True:
        ret, frame = cap.read()
        if not ret: break
        if frame_idx % interval == 0:
            yield frame, frame_idx / native_fps
        frame_idx += 1
    cap.release()


def classify_frame(frame):
    gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
    edges = cv2.Canny(gray, 50, 150)
    density = float(np.count_nonzero(edges) / edges.size)
    if density > 0.04: return "ppt"
    elif density > 0.01: return "code"
    else: return "ui_demo"


def make_frame_name(frame_type, timestamp_sec):
    ts_ms = int(timestamp_sec * 1000)
    uid = uuid.uuid4().hex[:8]
    return f"{frame_type}_{ts_ms:08d}_{uid}.jpg"


def build_pipeline(video_path, sample_fps=2.5):
    extractor = SmartKeyframeExtractor(resize=(480, 270), fps_sample=sample_fps)
    for frame, timestamp_sec in frame_sampler(video_path, sample_fps=sample_fps):
        output_frame, _ = extractor.process_frame(frame, timestamp_sec * 1000)
        if output_frame is not None:
            yield output_frame, timestamp_sec


def main():
    if len(sys.argv) < 2:
        print("用法: python3 video_keyframe_detector.py <video_path> [output_dir]")
        sys.exit(1)
    VIDEO_FILE = sys.argv[1]
    OUTPUT_DIR = sys.argv[2] if len(sys.argv) > 2 else "./keyframes"
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    print(f"🚀 Phase 5.0 处理: {VIDEO_FILE}")
    start_time = time.time()
    saved_count = 0
    for frame, ts in build_pipeline(VIDEO_FILE, sample_fps=2.5):
        frame_type = classify_frame(frame)
        name = make_frame_name(frame_type, ts)
        cv2.imwrite(os.path.join(OUTPUT_DIR, name), frame)
        saved_count += 1
        print(f"  ✓ [{saved_count:3d}] {frame_type:8s} {ts:7.1f}s -> {name}")
    elapsed = time.time() - start_time
    print(f"\n✨ 完成！{saved_count} 帧，耗时 {elapsed:.1f}s")


if __name__ == "__main__":
    main()
