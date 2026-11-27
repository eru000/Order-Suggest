"""節點 B：受控遮蔽實驗的影像產生器（期末規劃，回應委員5-3「1?0」）。

委員的問題：價格看不清時，OCR 會留下「?」讓流程中斷；VLM 可能自己補成一個合理的數字，
推薦就建立在錯價上。這裡在開發影像上「故意」讓幾個價格看不清，再量：
  - 模型正確回報未知（abstain）
  - 模型補出一個合法但錯誤的數字（hallucination）
  - 交叉複核有沒有抓到那個錯價

處理方式（每張衍生圖同時處理 k 個品項；同一張原圖的所有衍生圖處理同一批品項）
  mask_full    以價格框邊緣的背景色蓋住價格中的一位數字
  blur_partial / blur_full    價格區域高斯模糊（輕／重）
  glare_partial / glare_full  價格位置疊白色橢圓（半透明／幾乎不透明），模擬反光

價格位置：由 OCR（ocr_baseline）提出，且只採用「OCR 讀到的價格與真值相同」的品項，
確保框到的是正確的價格；另輸出每個遮蔽位置的放大預覽圖供人工確認（規劃要求）。

真值改寫：被處理的品項保留 true_price 與 occlusion 資訊；full 等級的期望價格改為 null
（看不到就應該回報未知），partial 等級保留原價（輕度干擾下讀對是合理的）。

固定亂數種子；同樣輸入重跑兩次，輸出影像與真值的雜湊相同。
執行：python -X utf8 evals/occlusion/make_variants.py --split dev
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import random
import sys
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
sys.path.insert(0, str(ROOT / "evals" / "menu_ocr"))
sys.path.insert(0, str(ROOT / "src"))

from PIL import Image, ImageDraw, ImageFilter  # noqa: E402

SEED = 20261127
ITEMS_PER_IMAGE = 4
SOURCES = {"dev": ROOT / "菜單照片資料" / "第二項" / "開發原圖"}
TREATMENTS = ("mask_full", "blur_partial", "blur_full", "glare_partial", "glare_full")


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _pad(box: tuple[float, float, float, float], image: Image.Image, ratio: float = 0.15) -> tuple[int, int, int, int]:
    x0, y0, x1, y1 = box
    pad_x, pad_y = (x1 - x0) * ratio, (y1 - y0) * ratio
    return (
        max(0, int(x0 - pad_x)), max(0, int(y0 - pad_y)),
        min(image.width, int(x1 + pad_x + 1)), min(image.height, int(y1 + pad_y + 1)),
    )


def _background(image: Image.Image, box: tuple[int, int, int, int]) -> tuple[int, int, int]:
    """價格框外緣一圈像素的中位數，當作底色。"""
    x0, y0, x1, y1 = box
    pixels = []
    for x in range(x0, x1):
        for y in (max(0, y0 - 2), min(image.height - 1, y1 + 1)):
            pixels.append(image.getpixel((x, y)))
    for y in range(y0, y1):
        for x in (max(0, x0 - 2), min(image.width - 1, x1 + 1)):
            pixels.append(image.getpixel((x, y)))
    channels = list(zip(*pixels))
    return tuple(sorted(channel)[len(channel) // 2] for channel in channels)  # type: ignore[return-value]


def apply(image: Image.Image, treatment: str, target: dict[str, Any]) -> None:
    box = tuple(target["priceBox"])
    region = _pad(box, image)  # type: ignore[arg-type]
    if treatment == "mask_full":
        digits = target["priceText"]
        x0, y0, x1, y1 = box
        width = (x1 - x0) / max(1, len(digits))
        index = target["maskDigit"]
        digit_box = _pad((x0 + index * width, y0, x0 + (index + 1) * width, y1), image, 0.1)
        ImageDraw.Draw(image).rectangle(digit_box, fill=_background(image, region))
    elif treatment.startswith("blur"):
        height = region[3] - region[1]
        radius = max(1.5, height * (0.18 if treatment == "blur_partial" else 0.45))
        patch = image.crop(region).filter(ImageFilter.GaussianBlur(radius))
        image.paste(patch, region[:2])
    elif treatment.startswith("glare"):
        alpha = 150 if treatment == "glare_partial" else 255
        x0, y0, x1, y1 = region
        cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
        rx, ry = (x1 - x0) * 0.75, (y1 - y0) * 0.95
        overlay = Image.new("RGBA", image.size, (255, 255, 255, 0))
        ImageDraw.Draw(overlay).ellipse((cx - rx, cy - ry, cx + rx, cy + ry), fill=(255, 255, 255, alpha))
        image.paste(Image.alpha_composite(image.convert("RGBA"), overlay).convert("RGB"))
    else:
        raise ValueError(treatment)


def eligible_targets(case: str, image_bytes: bytes, truth: list[dict[str, Any]]) -> list[dict[str, Any]]:
    import ocr_baseline
    from run_eval import match_items

    ocr_items = [i for i in ocr_baseline.pair_items(ocr_baseline.ocr_lines(image_bytes)) if i.get("priceBox")]
    fixed_truth = [{"name": row["name"], "price": row["price"]} for row in truth if row.get("price") is not None]
    matching = match_items(fixed_truth, [{"name": i["dish"], "price": i["price"]} for i in ocr_items])
    by_dish = {i["dish"]: i for i in ocr_items}
    targets = []
    for match in matching["matches"]:
        ocr_item = by_dish[match["predicted"]]
        try:
            agrees = abs(float(ocr_item["price"]) - float(match["expectedPrice"])) < 0.01
        except (TypeError, ValueError):
            agrees = False
        if agrees:
            targets.append({
                "name": match["expected"],
                "truePrice": match["expectedPrice"],
                "priceText": str(int(float(match["expectedPrice"]))),
                "priceBox": [round(v, 1) for v in ocr_item["priceBox"]],
            })
    return sorted(targets, key=lambda t: t["name"])


def build(split: str, out_dir: Path) -> dict[str, Any]:
    source = SOURCES[split]
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "preview").mkdir(exist_ok=True)
    manifest: dict[str, Any] = {"seed": SEED, "itemsPerImage": ITEMS_PER_IMAGE, "treatments": TREATMENTS,
                                "split": split, "cases": []}
    for truth_path in sorted(source.glob("*.expected.json")):
        case = truth_path.name.removesuffix(".expected.json")
        image_path = source / f"{case}.png"
        if not image_path.is_file():
            continue
        image_bytes = image_path.read_bytes()
        truth_doc = json.loads(truth_path.read_text(encoding="utf-8"))
        targets = eligible_targets(case, image_bytes, truth_doc["items"])
        rng = random.Random(f"{SEED}-{case}")
        chosen = sorted(rng.sample(targets, min(ITEMS_PER_IMAGE, len(targets))), key=lambda t: t["name"])
        for target in chosen:
            target["maskDigit"] = rng.randrange(len(target["priceText"]))
        record = {"case": case, "sourceSha256": sha256(image_bytes), "eligible": len(targets),
                  "targets": chosen, "variants": []}
        if chosen:
            for treatment in TREATMENTS:
                image = Image.open(io.BytesIO(image_bytes)).convert("RGB")
                for target in chosen:
                    apply(image, treatment, target)
                name = f"{case}__{treatment}"
                buffer = io.BytesIO()
                image.save(buffer, "PNG")
                (out_dir / f"{name}.png").write_bytes(buffer.getvalue())
                full = treatment.endswith("_full")
                chosen_names = {t["name"]: t for t in chosen}
                items = []
                for row in truth_doc["items"]:
                    target = chosen_names.get(row["name"])
                    if target is None:
                        items.append(row)
                        continue
                    items.append({
                        **row,
                        "price": None if full else row["price"],
                        "true_price": row["price"],
                        "occlusion": {"type": treatment.split("_")[0], "level": "full" if full else "partial",
                                      "box": target["priceBox"],
                                      **({"maskDigit": target["maskDigit"]} if treatment == "mask_full" else {})},
                    })
                truth_out = {"items": items, "derivedFrom": case, "treatment": treatment, "seed": SEED}
                truth_bytes = json.dumps(truth_out, ensure_ascii=False, indent=2).encode("utf-8")
                (out_dir / f"{name}.expected.json").write_bytes(truth_bytes)
                record["variants"].append({"name": name, "imageSha256": sha256(buffer.getvalue()),
                                           "truthSha256": sha256(truth_bytes)})
                # 放大預覽：每個被處理的價格框，供人工確認 full 等級確實看不出數字
                for index, target in enumerate(chosen):
                    crop = image.crop(_pad(tuple(target["priceBox"]), image, 1.5))  # type: ignore[arg-type]
                    crop = crop.resize((crop.width * 3, crop.height * 3))
                    crop.save(out_dir / "preview" / f"{name}__{index}.png")
        manifest["cases"].append(record)
        print(f"  {case:28} 可用價格框 {len(targets):3}，處理 {len(chosen)} 項")
    (out_dir / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    return manifest


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--split", choices=sorted(SOURCES), default="dev")
    parser.add_argument("--out", type=Path, default=None)
    args = parser.parse_args()
    out = args.out or HERE / args.split
    manifest = build(args.split, out)
    total = sum(len(c["targets"]) for c in manifest["cases"])
    print(f"共 {sum(len(c['variants']) for c in manifest['cases'])} 張衍生圖、{total} 個價格位置 → {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
