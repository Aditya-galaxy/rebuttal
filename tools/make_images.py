"""Generate the demo shop's images with Gemini on Vertex AI (gemini-2.5-flash-image).

Ember & Clay is a fictional studio, so its product photos, banner and the buyer's photo for the
chai-cup dispute are all generated. The buyer's photo is made from the listing photo, so it shows
the same cups, as a customer would photograph them at home.

Usage: VERTEX_PROJECT=<project> uv run python tools/make_images.py [name ...]
"""

import subprocess
import sys
import tempfile
from pathlib import Path

from rebuttal import vertex

OUT = Path(__file__).resolve().parent.parent / "rebuttal" / "static" / "img"
STYLE = ("Product photograph for a small handmade ceramics studio's online shop. Soft natural daylight from a "
         "window on the left, warm off-white linen backdrop, shallow depth of field, true-to-life colours, no "
         "text, no logos, no watermark, no people. Square framing with the object centred.")

PROMPTS = {
    "vase-indigo": f"{STYLE} A wheel-thrown stoneware vase about 32 cm tall with a deep indigo crackle glaze "
                   "and a slightly uneven hand-thrown rim.",
    "chai-rust": f"{STYLE} A set of four small handmade chai cups (150 ml each) in a rust-orange glaze with "
                 "subtle variation from cup to cup, arranged on a plain wooden tray.",
    "coaster-block": f"{STYLE} Six round hand-painted terracotta coasters with Indian block-print style motifs "
                     "in white and indigo, fanned out slightly, with cork backs visible on one.",
    "planter-pair": f"{STYLE} Two unglazed terracotta planters, one 18 cm and one 24 cm, each on a matching "
                    "saucer, empty, side by side.",
    "studio": "A wide photograph of a small ceramics studio workbench in Jaipur in warm morning light: a "
              "potter's wheel, shelves of drying bowls and cups, glaze buckets, terracotta and indigo pieces. "
              "No people, no text, no logos. Calm, documentary style, landscape framing.",
}
# The buyer's photo is an edit of the listing photo: same cups, shot at home.
BUYER = ("buyer-chai-rust", "chai-rust",
         "The same four cups photographed by a customer on a kitchen counter at home with a phone, under warm "
         "indoor lighting in the evening. Keep the cups' shapes identical. Under this light the glaze looks "
         "darker and browner than in the shop photo. Slightly casual framing, ordinary kitchen background, "
         "no people, no text.")


def save(name: str, data: bytes, width: int) -> None:
    with tempfile.NamedTemporaryFile(suffix=".png") as raw:
        raw.write(data)
        raw.flush()
        out = OUT / f"{name}.jpg"
        subprocess.run(["sips", "-s", "format", "jpeg", "-s", "formatOptions", "82", "--resampleWidth", str(width),
                        raw.name, "--out", str(out)], check=True, capture_output=True)
    print(name, out.stat().st_size // 1024, "KB")


def main(only: set[str]) -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    for name, prompt in PROMPTS.items():
        if not only or name in only:
            save(name, vertex.image_of(vertex.generate(vertex.IMAGE_MODEL, [{"text": prompt}], image=True)),
                 1600 if name == "studio" else 900)
    name, source, prompt = BUYER
    if not only or name in only:
        listing = (OUT / f"{source}.jpg").read_bytes()
        image = vertex.image_of(vertex.generate(vertex.IMAGE_MODEL, [vertex.inline(listing), {"text": prompt}],
                                                image=True))
        save(name, image, 900)


if __name__ == "__main__":
    main(set(sys.argv[1:]))
