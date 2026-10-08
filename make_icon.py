from pathlib import Path

from PIL import Image, ImageDraw

SIZE = 1024
OUT = Path(__file__).parent / "lockbox" / "assets"


def build():
    teal = (13, 148, 136, 255)
    amber = (251, 191, 36, 255)
    clear = (0, 0, 0, 0)

    art = Image.new("RGBA", (SIZE, SIZE), clear)
    draw = ImageDraw.Draw(art)

    cx, top = 512, 120
    outer, inner = 232, 138
    draw.ellipse((cx - outer, top, cx + outer, top + outer * 2), fill=amber)
    draw.ellipse((cx - inner, top + (outer - inner), cx + inner, top + (outer - inner) + inner * 2), fill=clear)
    draw.rectangle((0, top + outer, SIZE, SIZE), fill=clear)
    draw.rectangle((cx - outer, top + outer, cx - inner, 560), fill=amber)
    draw.rectangle((cx + inner, top + outer, cx + outer, 560), fill=amber)

    draw.rounded_rectangle((190, 500, 834, 930), radius=100, fill=teal)
    draw.ellipse((512 - 70, 650 - 70, 512 + 70, 650 + 70), fill=clear)
    draw.polygon([(478, 690), (546, 690), (562, 820), (462, 820)], fill=clear)

    box = art.getbbox()
    art = art.crop(box)
    side = max(art.size)
    margin = int(side * 0.06)
    canvas = Image.new("RGBA", (side + margin * 2, side + margin * 2), clear)
    canvas.paste(art, ((canvas.width - art.width) // 2, (canvas.height - art.height) // 2))
    return canvas


if __name__ == "__main__":
    OUT.mkdir(parents=True, exist_ok=True)
    icon = build()
    icon.resize((256, 256), Image.LANCZOS).save(OUT / "icon.png")
    icon.save(OUT / "icon.ico", sizes=[(256, 256), (128, 128), (64, 64), (48, 48), (32, 32), (24, 24), (16, 16)])

_credit = "Built by Unkn0wngb"
