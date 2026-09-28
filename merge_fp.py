#!/usr/bin/env python3
"""发票 + 付款截图 自动拼版工具

用法: python3 merge_fp.py [材料目录]     (默认 ~/Downloads/fp)

流程:
1. 扫描目录中的发票 PDF 与付款截图，按文件名前缀自动分组（去尾部数字），
   组内发票在前、截图按编号排序，两两配对拼版
2. 调用视觉大模型识别截图中"金额/商户/支付时间"关键区域并智能裁剪
3. 生成 A4 拼版 PDF 到 ~/Downloads（每页上下两联，高度各自占满半页），
   完成后用 Chrome 打开

环境变量（可写入脚本同目录 .env，兼容任何 OpenAI 兼容接口）:
  AI_API_KEY    API Key（智谱: https://open.bigmodel.cn ；留空则用默认裁剪）
  AI_BASE_URL   默认 https://open.bigmodel.cn/api/paas/v4
  AI_MODEL      默认 glm-5.3-flash
"""
import base64
import io
import json
import os
import re
import shutil
import subprocess
import sys
import urllib.request
import warnings
from pathlib import Path

warnings.filterwarnings(
    "ignore", message=r"Python 3\.\d+ is no longer supported.*")

from PIL import Image
from pypdf import PdfReader, PdfWriter, Transformation

# ---------- 配置 ----------
SCRIPT_DIR = Path(__file__).resolve().parent
HOME = Path.home()

A4_W, A4_H = 595.276, 841.890   # pt
HALF_H = A4_H / 2
MARGIN = 8                      # 半栏内边距(pt)，避开打印机不可打印区
DPI = 300
PX = DPI / 72.0                 # px per pt

IMG_EXTS = {".jpg", ".jpeg", ".png", ".webp"}
DEFAULT_CROP = (8, 50)          # AI 不可用时按图片高度百分比兜底

PROMPT = (
    "这是一张手机支付/交易详情截图。请找出包含关键信息的区域：商户或收款方名称、"
    "金额、交易状态、支付/交易时间、支付方式、商品说明等字段。"
    "排除顶部手机状态栏和导航栏，也排除底部无关内容（如推荐服务、账单管理、"
    "积分领取、联系商家等）。"
    '只返回严格 JSON，格式：{"top": 关键区域起始处占整图高度的百分比, '
    '"bottom": 关键区域结束处占整图高度的百分比}，数字为 0-100。'
)


def load_env():
    env_file = SCRIPT_DIR / ".env"
    if not env_file.exists():
        return
    for line in env_file.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        k, v = k.strip(), v.strip().strip("'\"")
        if k and v and k not in os.environ:
            os.environ[k] = v


def ai_settings():
    return (
        os.environ.get("AI_API_KEY", ""),
        os.environ.get("AI_BASE_URL", "https://open.bigmodel.cn/api/paas/v4").rstrip("/"),
        os.environ.get("AI_MODEL", "glm-5.3-flash"),
    )


# ---------- 扫描分组 ----------
def trailing_num(stem: str) -> int:
    m = re.search(r"(\d+)$", stem)
    return int(m.group(1)) if m else -1


def group_key(stem: str) -> str:
    return re.sub(r"\d+$", "", stem) or stem


def discover(src: Path, exclude: set):
    pdfs, shots = {}, {}
    for f in sorted(src.iterdir()):
        if not f.is_file() or f.name in exclude or f.name.startswith("."):
            continue
        ext = f.suffix.lower()
        if ext == ".pdf":
            pdfs.setdefault(group_key(f.stem), []).append(f)
        elif ext in IMG_EXTS:
            shots.setdefault(group_key(f.stem), []).append(f)
    keys = sorted(set(pdfs) | set(shots))
    groups = []
    for k in keys:
        items = sorted(pdfs.get(k, [])) + sorted(
            shots.get(k, []), key=lambda f: (trailing_num(f.stem), f.name)
        )
        groups.append((k, [i.name for i in items]))
    return groups


# ---------- AI 裁剪 ----------
def _parse_crop_text(text: str):
    """从模型返回文本中提取扁平 JSON {"top": x, "bottom": y}（百分比）。"""
    m = re.search(r"\{[^{}]*\}", text, re.S)
    if not m:
        return None
    try:
        box = json.loads(m.group())
        return float(box["top"]), float(box["bottom"])
    except Exception:
        return None


def ask_ai_crop(img: Image.Image, name: str):
    key, base, model = ai_settings()
    if not key:
        return None, "未配置 AI_API_KEY"
    im = img.copy()
    im.thumbnail((1600, 1600))
    buf = io.BytesIO()
    im.save(buf, "JPEG", quality=85)
    b64 = base64.b64encode(buf.getvalue()).decode()
    payload = {
        "model": model,
        "temperature": 0.1,
        "max_tokens": 4096,
        "messages": [{
            "role": "user",
            "content": [
                {"type": "image_url",
                 "image_url": {"url": f"data:image/jpeg;base64,{b64}"}},
                {"type": "text", "text": PROMPT},
            ],
        }],
    }
    req = urllib.request.Request(
        f"{base}/chat/completions",
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json",
                 "Authorization": f"Bearer {key}"},
    )
    last_err = None
    for attempt in (1, 2):  # 思考型模型偶发空回复，重试一次
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                data = json.loads(resp.read())
            msg = data["choices"][0]["message"]
            text = (msg.get("content") or "") + "\n" + (msg.get("reasoning_content") or "")
            box = _parse_crop_text(text)
            if box is None:
                raise ValueError(f"回复中无有效 JSON: {text[:120]!r}")
        except Exception as e:
            last_err = f"{type(e).__name__}: {e}"
            continue
        if not (0 <= box[0] < box[1] <= 100):
            return None, f"模型返回越界 top={box[0]} bottom={box[1]}"
        return box, None
    return None, last_err


def cropped_image(path: Path):
    img = Image.open(path).convert("RGB")
    box, err = ask_ai_crop(img, path.name)
    if box is None:
        box = DEFAULT_CROP
        print(f"  [兜底] {path.name}: {err}，使用默认裁剪 {DEFAULT_CROP}")
    else:
        print(f"  [AI] {path.name}: 裁剪 {box[0]:.0f}% ~ {box[1]:.0f}%")
    w, h = img.size
    return img.crop((0, round(h * box[0] / 100), w, round(h * box[1] / 100)))


# ---------- 拼版 ----------
def paste_in_slot(page: Image.Image, img: Image.Image, slot: int):
    """slot: 0=上半栏, 1=下半栏。高度撑满半栏、水平居中。"""
    half_px = round(HALF_H * PX)
    margin_px = round(MARGIN * PX)
    target_h = half_px - 2 * margin_px
    w, h = img.size
    target_w = round(target_h * w / h)
    if target_w > page.width - 2 * margin_px:
        target_w = page.width - 2 * margin_px
        target_h = round(target_w * h / w)
    img = img.resize((target_w, target_h), Image.LANCZOS)
    x = (page.width - target_w) // 2
    y = margin_px if slot == 0 else half_px + margin_px
    y += max(0, (half_px - 2 * margin_px - target_h) // 2)
    page.paste(img, (x, y))


def compose_page(chunk):
    page = Image.new("RGB", (round(A4_W * PX), round(A4_H * PX)), "white")
    for slot, name in enumerate(chunk):
        if name.lower().endswith(".pdf"):
            continue  # 发票位置留白，稍后矢量合并
        paste_in_slot(page, cropped_image(SRC_DIR / name), slot)
    return page


def build(src: Path, out: Path):
    groups = discover(src, exclude={out.name})
    if not groups:
        sys.exit(f"目录 {src} 中没有找到 PDF 或图片")
    global SRC_DIR
    SRC_DIR = src

    pages, invoice_slots = [], []
    print(f"分组结果: " + ", ".join(f"{k}({len(v)}项)" for k, v in groups))
    for _, items in groups:
        for i in range(0, len(items), 2):
            chunk = items[i:i + 2]
            pages.append(compose_page(chunk))
            for slot, name in enumerate(chunk):
                if name.lower().endswith(".pdf"):
                    invoice_slots.append((len(pages), name, slot))

    print(f"共 {len(pages)} 页，开始合成 PDF ...")
    buf = io.BytesIO()
    pages[0].save(buf, "PDF", save_all=True, append_images=pages[1:],
                  resolution=DPI)
    buf.seek(0)

    reader = PdfReader(buf)
    writer = PdfWriter()
    for p in reader.pages:
        writer.add_page(p)

    inv_cache = {}
    for page_no, name, slot in invoice_slots:
        if name not in inv_cache:
            inv_cache[name] = PdfReader(src / name).pages[0]
        inv = inv_cache[name]
        iw, ih = float(inv.mediabox.width), float(inv.mediabox.height)
        s = min((A4_W - 2 * MARGIN) / iw, (HALF_H - 2 * MARGIN) / ih)
        tx = (A4_W - iw * s) / 2
        ty = ((A4_H - HALF_H) if slot == 0 else 0) + (HALF_H - ih * s) / 2
        writer.pages[page_no - 1].merge_transformed_page(
            inv, Transformation().scale(s).translate(tx, ty))

    with open(out, "wb") as f:
        writer.write(f)
    print(f"已生成 {out}（{len(pages)} 页）")


def open_in_chrome(path: Path):
    for browser in ("google-chrome", "google-chrome-stable",
                    "chromium", "chromium-browser"):
        exe = shutil.which(browser)
        if exe:
            subprocess.Popen([exe, str(path)],
                             stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL)
            print(f"已在 {browser} 中打开")
            return
    print("未找到 Chrome/Chromium，请手动打开:", path)


def main():
    load_env()
    src = Path(sys.argv[1]).resolve() if len(sys.argv) > 1 else HOME / "Downloads/fp"
    if not src.is_dir():
        sys.exit(f"目录不存在: {src}")
    out = HOME / "Downloads" / f"{src.name}_合并.pdf"
    build(src, out)
    open_in_chrome(out)


if __name__ == "__main__":
    main()
