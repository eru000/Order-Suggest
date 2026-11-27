"""Build and verify the local, frozen Step 2 experiment from source annotations."""

import argparse
import hashlib
import io
import json
import math
from collections import Counter, defaultdict
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter

ROOT = Path(__file__).resolve().parents[2]
STUDY = Path(__file__).resolve().parent
PHOTOS = ROOT / "菜單照片資料" / "第二項"
LEGACY = ROOT / "evals/menu_ocr/cases"
SPLITS = {"dev": "開發原圖", "holdout": "獨立封存", "stress": "開發合成干擾"}
LEGACY_NAMES = {
    "budaoweng": "不倒翁", "douliumen": "斗六門", "guoshao_6col": "國紹",
    "xianghong": "向宏魯肉飯", "web_dalai_noodles_soups": "大來小館",
    "web_fhgh_sets": "名人堂花園大飯店",
}


def sha(data):
    return hashlib.sha256(data).hexdigest()


def encoded(value):
    return (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode()


def frozen_write(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        if path.read_bytes() != data:
            raise ValueError(f"Frozen file differs: {path}")
        return
    with path.open("xb") as stream:
        stream.write(data)


def disruptions(data):
    """Single-factor changes at original size, including padding for rotation."""
    with Image.open(io.BytesIO(data)) as original:
        im = original.convert("RGB")
    w, h = im.size
    small = im.resize((max(1, w // 3), max(1, h // 3)), Image.Resampling.BOX)
    mask = Image.new("L", im.size)
    draw = ImageDraw.Draw(mask)
    draw.ellipse((w * .38, h * -.35, w * .64, h * 1.35), fill=156)
    mask = mask.filter(ImageFilter.GaussianBlur(max(1, w * .035)))
    variants = {
        "lowres": small.resize(im.size, Image.Resampling.BILINEAR),
        "tilt": im.rotate(8, Image.Resampling.BICUBIC, expand=True, fillcolor="white"),
        "glare": Image.composite(Image.new("RGB", im.size, "white"), im, mask),
    }
    for suffix, result in variants.items():
        buf = io.BytesIO()
        result.save(buf, format="PNG")
        yield suffix, buf.getvalue()


def materialize():
    records = []

    def add(case_id, split, image, truth, source, truth_path, parent=None):
        folder = PHOTOS / SPLITS[split]
        labels = truth["items"]
        if not labels:
            raise ValueError(f"No labels in {case_id}")
        for item in labels:
            price = item["price"]
            if not item["name"].strip() or (price is not None and
                    (type(price) not in (float, int) or not math.isfinite(price) or price < 0)):
                raise ValueError(f"Invalid item in {case_id}")
        expected = dict(truth, items=[dict(item) for item in labels])
        # The product encodes real selectable sizes in the name field. Do not
        # fabricate suffixes for slash-separated prices with no printed labels.
        for item in expected["items"]:
            variant = item.get("variant")
            if variant and not variant.startswith("unlabelled price option"):
                item["name"] += f"（{variant}）"
        truth_bytes = encoded(expected)
        frozen_write(folder / f"{case_id}.expected.json", truth_bytes)
        if image is not None:
            with Image.open(io.BytesIO(image)) as im:
                im.load()
                dimensions = list(im.size)
            frozen_write(folder / f"{case_id}.png", image)
        records.append(dict(id=case_id, split=split, restaurant=source["restaurant"],
                            group=source["group"], source=source, parent=parent,
                            annotation=str(truth_path.relative_to(ROOT).as_posix()),
                            annotationSha256=sha(truth_path.read_bytes()),
                            inputSha256=sha(image) if image is not None else None,
                            dimensions=dimensions if image is not None else None,
                            expectedSha256=sha(truth_bytes), items=len(labels),
                            unknown=sum(i["price"] is None for i in labels)))

    for case_id, restaurant in LEGACY_NAMES.items():
        path = LEGACY / f"{case_id}.expected.json"
        truth = json.loads(path.read_bytes())
        image = LEGACY / f"{case_id}.png"
        add(case_id, "dev", image.read_bytes() if image.is_file() else None, truth,
            dict(restaurant=restaurant, group=case_id, **truth.get("source", {}))
            if not isinstance(truth.get("source"), dict) or "group" not in truth["source"]
            else dict(truth["source"], restaurant=restaurant, group=case_id), path)

    for path in sorted((STUDY / "menus").glob("*/*/truth.json")):
        folder = path.parent
        source = json.loads((folder / "source.json").read_bytes())
        data = (folder / "input.png").read_bytes()
        if sha(data) != source["inputSha256"]:
            raise ValueError(f"Source hash mismatch: {folder.name}")
        truth = json.loads(path.read_bytes())
        split = folder.parent.name
        add(folder.name, split, data, truth, source, path)
        if split == "dev":
            for effect, changed in disruptions(data):
                add(f"{folder.name}_{effect}", "stress", changed, truth,
                    dict(source, syntheticEffect=effect), path, folder.name)

    validate(records)
    frozen_write(PHOTOS / "manifest.json", encoded(records))
    return records


def validate(records):
    seen = defaultdict(dict)
    parents = {r["id"]: r for r in records if r["split"] == "dev"}
    for r in records:
        folder = PHOTOS / SPLITS[r["split"]]
        for p, digest in [(folder / (r["id"] + ".expected.json"), r["expectedSha256"]),
                          (folder / (r["id"] + ".png"), r["inputSha256"]),
                          (ROOT / r["annotation"], r["annotationSha256"])]:
            if (sha(p.read_bytes()) if p.is_file() else None) != digest:
                raise ValueError(f"Hash mismatch: {p}")
        parent = r["parent"]
        if parent and (parent not in parents or parents[parent]["group"] != r["group"] or
                       parents[parent]["expectedSha256"] != r["expectedSha256"]):
            raise ValueError(f"Invalid derivative: {r['id']}")
        split = "holdout" if r["split"] == "holdout" else "dev"
        src = r["source"]
        for key, value in [("group", r["group"]), ("restaurant", r["restaurant"]),
                           ("url", src.get("url")), ("input", r["inputSha256"]),
                           ("origin", src.get("sourceSha256") or src.get("rawSha256") or
                            src.get("pdfSha256") or src.get("originalSha256"))]:
            if value and seen[key].setdefault(value, split) != split:
                raise ValueError(f"Cross-split {key}: {value}")
    for split, dirname in SPLITS.items():
        subset = [r for r in records if r["split"] == split]
        if {p.stem for p in (PHOTOS / dirname).glob("*.png")} != {
            r["id"] for r in subset if r["inputSha256"]
        } or {p.name.removesuffix(".expected.json") for p in
              (PHOTOS / dirname).glob("*.expected.json")} != {r["id"] for r in subset}:
            raise ValueError(f"Unregistered/missing files: {dirname}")
    counts = Counter(r["split"] for r in records)
    if counts != {"dev": 10, "holdout": 12, "stress": 12}:
        raise ValueError(f"Unexpected case counts: {counts}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--prepare", action="store_true", help="create frozen inputs once")
    args = parser.parse_args()
    records = materialize() if args.prepare else json.loads((PHOTOS / "manifest.json").read_bytes())
    validate(records)
    for split in SPLITS:
        subset = [r for r in records if r["split"] == split]
        available = [r for r in subset if r["inputSha256"]]
        print(split, "images", len(available), "/", len(subset),
              "scorable rows", sum(r["items"] for r in available))
