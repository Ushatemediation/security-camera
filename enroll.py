"""Регистрация известных лиц: фото из known/ -> усреднённый эмбеддинг -> embedding.npy."""

import sys
from pathlib import Path

import cv2
import numpy as np
import yaml

BASE_DIR = Path(__file__).resolve().parent
KNOWN_DIR = BASE_DIR / "known"


def main():
    with open(BASE_DIR / "config.yaml", encoding="utf-8") as f:
        cfg = yaml.safe_load(f)

    from insightface.app import FaceAnalysis

    app = FaceAnalysis(name="buffalo_l", providers=["CPUExecutionProvider"])
    app.prepare(ctx_id=-1, det_size=tuple(cfg["face"]["det_size"]))

    emb_data: dict[str, np.ndarray] = {}
    if (KNOWN_DIR / "embedding.npy").exists():
        emb_data = np.load(KNOWN_DIR / "embedding.npy", allow_pickle=True).item()

    photos = sorted(p for p in KNOWN_DIR.iterdir()
                    if p.suffix.lower() in (".jpg", ".jpeg", ".png") and p.name != "embedding.npy")
    if not photos:
        print("Нет фото в known/ — положите туда фото лиц")
        return

    for photo in photos:
        name = photo.stem
        img = cv2.imread(str(photo))
        if img is None:
            print(f"[ПРОПУСК] {photo.name}: не читается")
            continue
        faces = app.get(img)
        if len(faces) == 0:
            print(f"[ПРОПУСК] {photo.name}: лицо не найдено")
            continue
        if len(faces) > 1:
            print(f"[ПРОПУСК] {photo.name}: найдено лиц {len(faces)} — нужно одно")
            continue
        emb = faces[0].normed_embedding
        if name in emb_data:
            # усредняем с существующим
            emb = (emb_data[name] + emb) / 2
            emb = emb / np.linalg.norm(emb)
        emb_data[name] = emb
        print(f"[OK] {name}: эмбеддинг сохранён")

    np.save(KNOWN_DIR / "embedding.npy", emb_data)
    print(f"Готово. Всего лиц: {len(emb_data)} -> known/embedding.npy")


if __name__ == "__main__":
    main()
