"""Подготовка страницы скана перед Tesseract.

Рендер в оттенках серого 300 dpi → тёмные полосы у краёв (до 15% ширины) в белое →
поворот на 0/90/180/270° по определению ориентации Tesseract (на изображении вдвое меньше) →
выравнивание наклона перебором угла ±3° с шагом 0,1° по резкости горизонтального профиля,
если угол от 0,15° → стирание линий таблиц и рамок от 8 мм.

Каждый шаг, который двигает пиксели, дописывает своё преобразование в матрицу 3×3:
рамки слов потом переводятся обратно в координаты страницы, иначе подсветка при
экспорте съедет.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

import cv2
import numpy as np
import pymupdf

from ml.ocr.tesseract import Orientation, detect_orientation

log = logging.getLogger(__name__)

EDGE_BAND = 0.15
EDGE_DARK = 110
DESKEW_RANGE = 3.0
DESKEW_STEP = 0.1
DESKEW_MIN = 0.15
LINE_MIN_MM = 8.0
OSD_MIN_CONFIDENCE = 0.5
OSD_SCALE = 0.5
OSD_RELIABLE = 5.0


@dataclass
class Prepared:
    image: np.ndarray
    dpi: int
    # Матрица из пикселей исходного рендера в пиксели подготовленного изображения
    matrix: np.ndarray = field(default_factory=lambda: np.eye(3))
    rotation: int = 0
    skew: float = 0.0

    def to_page(self, x0: float, y0: float, x1: float, y1: float) -> tuple[float, float, float, float]:
        """Рамка из подготовленного изображения → пункты страницы (система `page.rect`)."""
        inv = np.linalg.inv(self.matrix)
        corners = np.array([[x0, y0, 1], [x1, y0, 1], [x0, y1, 1], [x1, y1, 1]], dtype=float).T
        src = inv @ corners
        scale = 72 / self.dpi
        xs, ys = src[0] * scale, src[1] * scale
        return float(xs.min()), float(ys.min()), float(xs.max()), float(ys.max())


def render(page: pymupdf.Page, dpi: int) -> np.ndarray:
    pix = page.get_pixmap(dpi=dpi, colorspace=pymupdf.csGRAY, alpha=False)
    return np.frombuffer(pix.samples, dtype=np.uint8).reshape(pix.height, pix.stride)[:, : pix.width].copy()


def whiten_edges(img: np.ndarray) -> np.ndarray:
    """Тёмные полосы сканера у краёв (до 15% размера) → белое."""
    h, w = img.shape
    cols = img.mean(axis=0)
    rows = img.mean(axis=1)
    out = img
    limit_x, limit_y = int(w * EDGE_BAND), int(h * EDGE_BAND)

    def band(profile: np.ndarray, limit: int) -> int:
        n = 0
        while n < limit and profile[n] < EDGE_DARK:
            n += 1
        return n

    left, right = band(cols, limit_x), band(cols[::-1], limit_x)
    top, bottom = band(rows, limit_y), band(rows[::-1], limit_y)
    if left or right or top or bottom:
        out = img.copy()
        if left:
            out[:, :left] = 255
        if right:
            out[:, w - right :] = 255
        if top:
            out[:top, :] = 255
        if bottom:
            out[h - bottom :, :] = 255
        log.debug("ocr: тёмные края → белое: слева %d, справа %d, сверху %d, снизу %d px", left, right, top, bottom)
    return out


def _rotation_matrix(k: int, w: int, h: int) -> np.ndarray:
    """Матрица для np.rot90(img, k): поворот на k×90° против часовой."""
    k %= 4
    if k == 0:
        return np.eye(3)
    if k == 1:  # (x, y) → (y, w-1-x)
        return np.array([[0, 1, 0], [-1, 0, w - 1], [0, 0, 1]], dtype=float)
    if k == 2:
        return np.array([[-1, 0, w - 1], [0, -1, h - 1], [0, 0, 1]], dtype=float)
    return np.array([[0, -1, h - 1], [1, 0, 0], [0, 0, 1]], dtype=float)


def page_orientation(img: np.ndarray, dpi: int) -> Orientation | None:
    """Ориентация по изображению вдвое меньше: 0,3 с вместо 0,7 с на страницу. На 36 страницах
    синтетических сканов в четырёх поворотах ответ совпал с полным размером, уверенность 25–36
    против 38–45 (замер 27.10). Ненадёжный ответ перепроверяется на полном размере."""
    small = cv2.resize(img, None, fx=OSD_SCALE, fy=OSD_SCALE, interpolation=cv2.INTER_AREA)
    found = detect_orientation(small, dpi=round(dpi * OSD_SCALE))
    if found is not None and found.confidence >= OSD_RELIABLE:
        return found
    return detect_orientation(img, dpi=dpi)


def fix_orientation(prep: Prepared) -> Prepared:
    found = page_orientation(prep.image, prep.dpi)
    if found is None or found.rotate == 0 or found.confidence < OSD_MIN_CONFIDENCE:
        return prep
    # Tesseract сообщает, на сколько градусов повернуть по часовой; np.rot90 крутит против
    k = (-found.rotate // 90) % 4
    h, w = prep.image.shape
    prep.matrix = _rotation_matrix(k, w, h) @ prep.matrix
    prep.image = np.ascontiguousarray(np.rot90(prep.image, k))
    prep.rotation = found.rotate
    log.info("ocr: страница повёрнута на %d° (уверенность %.1f)", found.rotate, found.confidence)
    return prep


def estimate_skew(img: np.ndarray) -> float:
    """Угол наклона строк в градусах: перебор ±3° с шагом 0,1° по резкости профиля строк."""
    small_scale = 1000 / max(img.shape)
    small = cv2.resize(img, None, fx=small_scale, fy=small_scale, interpolation=cv2.INTER_AREA) if small_scale < 1 else img
    _, binary = cv2.threshold(small, 0, 255, cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU)
    ys, xs = np.nonzero(binary)
    if len(xs) < 500:
        return 0.0
    if len(xs) > 60000:
        idx = np.random.default_rng(0).choice(len(xs), 60000, replace=False)
        xs, ys = xs[idx], ys[idx]
    xs = xs.astype(float) - small.shape[1] / 2
    ys = ys.astype(float)
    best_angle, best_score = 0.0, -1.0
    height = small.shape[0] * 2
    for angle in np.arange(-DESKEW_RANGE, DESKEW_RANGE + 1e-9, DESKEW_STEP):
        rad = np.deg2rad(angle)
        proj = ys * np.cos(rad) - xs * np.sin(rad)
        hist = np.bincount(np.clip((proj + small.shape[0] / 2).astype(int), 0, height - 1), minlength=height)
        score = float(np.sum(np.diff(hist.astype(float)) ** 2))
        if score > best_score:
            best_score, best_angle = score, float(angle)
    return round(best_angle, 2)


def deskew(prep: Prepared) -> Prepared:
    angle = estimate_skew(prep.image)
    if abs(angle) < DESKEW_MIN:
        return prep
    h, w = prep.image.shape
    # estimate_skew возвращает угол в системе OpenCV (против часовой при y вниз): поворот на него выпрямляет строки
    m = cv2.getRotationMatrix2D((w / 2, h / 2), angle, 1.0)
    prep.image = cv2.warpAffine(prep.image, m, (w, h), flags=cv2.INTER_LINEAR, borderValue=255)
    prep.matrix = np.vstack([m, [0, 0, 1]]) @ prep.matrix
    prep.skew = angle
    log.debug("ocr: наклон %.2f° выровнен", angle)
    return prep


def remove_lines(img: np.ndarray, dpi: int) -> np.ndarray:
    """Стирает линии таблиц и рамок длиннее 8 мм: Tesseract читает их как «|», «_» и «—»."""
    length = max(10, int(LINE_MIN_MM / 25.4 * dpi))
    binary = cv2.adaptiveThreshold(img, 255, cv2.ADAPTIVE_THRESH_MEAN_C, cv2.THRESH_BINARY_INV, 31, 15)
    horizontal = cv2.morphologyEx(binary, cv2.MORPH_OPEN, cv2.getStructuringElement(cv2.MORPH_RECT, (length, 1)))
    vertical = cv2.morphologyEx(binary, cv2.MORPH_OPEN, cv2.getStructuringElement(cv2.MORPH_RECT, (1, length)))
    mask = cv2.dilate(cv2.bitwise_or(horizontal, vertical), np.ones((3, 3), np.uint8))
    if not mask.any():
        return img
    out = img.copy()
    out[mask > 0] = 255
    return out


def prepare_image(original: np.ndarray, dpi: int, *, orientation: bool = True) -> Prepared:
    """Подготовка рендера страницы. Без PyMuPDF, поэтому идёт в потоках вместе с Tesseract."""
    prep = Prepared(image=whiten_edges(original), dpi=dpi)
    if orientation:
        prep = fix_orientation(prep)
    prep = deskew(prep)
    prep.image = remove_lines(prep.image, dpi)
    return prep
