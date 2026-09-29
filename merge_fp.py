#!/usr/bin/env python3
"""发票 + 付款截图 自动拼版工具

用法: python3 merge_fp.py [材料目录] [--no-cache] [--code 编号]
  目录默认 ~/Downloads/fp
  --no-cache  忽略已有裁剪缓存，全部重新识别（新结果写回缓存）
  --code      编号（如 202609290007），印在每个半页右上角（便于对折裁剪后归档）

流程:
1. 扫描目录中的发票 PDF 与付款截图，按文件名前缀自动分组（去尾部数字），
   组内发票在前、截图按编号排序，两两配对拼版
2. 调用视觉大模型识别截图中"金额/商户/支付时间"关键区域并智能裁剪
3. 生成 A4 拼版 PDF 到 ~/Downloads（每页上下两联，高度各自占满半页），
   完成后用 Chrome 打开

环境变量（可写入脚本同目录 .env，兼容任何 OpenAI 兼容接口）:
  AI_API_KEY       API Key（智谱: https://open.bigmodel.cn ；留空则用默认裁剪）
  AI_BASE_URL      默认 https://open.bigmodel.cn/api/paas/v4
  AI_MODEL         默认 glm-5.3-flash
  AI_CONCURRENCY   截图识别并发数，默认 3（接口限流时可调小）
裁剪坐标按「文件路径+修改时间+大小」缓存在脚本同目录 .crop_cache.json，
文件未变时重跑不再调用接口。
"""
import argparse
import base64
import io
import json
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
import warnings
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal
from pathlib import Path

warnings.filterwarnings(
    "ignore", message=r"Python 3\.\d+ is no longer supported.*")

from PIL import Image, ImageDraw, ImageFont
from pypdf import PdfReader, PdfWriter, Transformation

# ---------- 配置 ----------
SCRIPT_DIR = Path(__file__).resolve().parent
HOME = Path.home()
CACHE_FILE = SCRIPT_DIR / ".crop_cache.json"

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


def ai_concurrency() -> int:
    return max(1, int(os.environ.get("AI_CONCURRENCY", "3")))


# ---------- 裁剪缓存 ----------
def cache_key(path: Path) -> str:
    st = path.stat()
    return f"{path.name}:{int(st.st_mtime)}:{st.st_size}"


def load_cache() -> dict:
    if CACHE_FILE.exists():
        try:
            return json.loads(CACHE_FILE.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {}


def save_cache(cache: dict):
    CACHE_FILE.write_text(
        json.dumps(cache, ensure_ascii=False, indent=1), encoding="utf-8")


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
    """返回 ((top, bottom) 百分比, None) 或 (None, 错误信息)。"""
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
    for attempt in range(3):
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                data = json.loads(resp.read())
            msg = data["choices"][0]["message"]
            text = (msg.get("content") or "") + "\n" + (msg.get("reasoning_content") or "")
            box = _parse_crop_text(text)
            if box is None:
                last_err = f"回复中无有效 JSON: {text[:120]!r}"
            elif not (0 <= box[0] < box[1] <= 100):
                return None, f"模型返回越界 top={box[0]} bottom={box[1]}"
            else:
                return box, None
        except urllib.error.HTTPError as e:
            snippet = e.read()[:150].decode(errors="replace") if e.fp else e.reason
            last_err = f"HTTP {e.code}: {snippet}"
            if e.code == 429 and attempt < 2:  # 接口限流：退避后重试
                wait = float(e.headers.get("Retry-After") or 2 * (attempt + 1))
                print(f"  [限流] {name}: 429，等待 {wait:.0f}s 后重试")
                time.sleep(wait)
                continue
        except Exception as e:
            last_err = f"{type(e).__name__}: {e}"
        if attempt < 2:
            time.sleep(1)
    return None, last_err


def crop_box_for(path: Path, cache: dict, use_cache: bool = True):
    """带缓存的裁剪坐标获取，供线程池并发调用。返回 (box, 来源标签)。"""
    key = cache_key(path)
    if use_cache and key in cache:
        return tuple(cache[key]), "缓存"
    img = Image.open(path).convert("RGB")
    box, err = ask_ai_crop(img, path.name)
    if box is None:
        w, h = img.size
        if 1.7 <= h / w <= 2.5:  # 手机屏幕竖长截图：关键信息基本在上半部
            box = (0, 50)
        else:
            box = DEFAULT_CROP
        return box, f"兜底({err})"
    cache[key] = list(box)
    return box, "AI"


def cropped_image(path: Path, box) -> Image.Image:
    img = Image.open(path).convert("RGB")
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


def compose_page(chunk, boxes, code=None):
    page = Image.new("RGB", (round(A4_W * PX), round(A4_H * PX)), "white")
    for slot, name in enumerate(chunk):
        if name.lower().endswith(".pdf"):
            continue  # 发票位置留白，稍后矢量合并
        paste_in_slot(page, cropped_image(SRC_DIR / name, boxes[name]), slot)
    if code:  # 每个半张的右上角：上半张=页面右上，下半张=折线右上（对折裁剪后各半张都有编号）
        for top_pt in (A4_H, HALF_H):
            draw_code(page, code, A4_W, top_pt)
    return page


FONT_CANDIDATES = (
    "/usr/share/fonts/TTF/DejaVuSans.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "/usr/share/fonts/dejavu/DejaVuSans.ttf",
    "/usr/share/fonts/TTF/DejaVuSans-Bold.ttf",
)


def get_font(size_px: int):
    for p in FONT_CANDIDATES:
        if Path(p).exists():
            return ImageFont.truetype(p, size_px)
    return ImageFont.load_default()


def draw_code(page: Image.Image, code: str, right_pt: float, top_pt: float):
    """在给定上边缘（pt 坐标）下方、页面右缘内侧画白底黑字编号标签。"""
    d = ImageDraw.Draw(page)
    font = get_font(round(11 * PX))
    pad = round(5 * PX)
    w = d.textlength(code, font=font)
    box_h = font.size + pad * 2
    x2 = round(right_pt * PX) - pad - round(10 * PX)
    y1 = round(page.height - top_pt * PX) + pad + round(6 * PX)
    box = (x2 - w - pad * 2, y1, x2, y1 + box_h)
    d.rounded_rectangle(box, radius=8, fill="white", outline=(0, 0, 0), width=3)
    d.text((box[0] + pad, y1 + pad), code, fill=(0, 0, 0), font=font)


AMOUNT_RE = re.compile(r"[¥￥]\s*([0-9,]+(?:\.[0-9]+)?)")


def extract_invoice_amount(page):
    """从发票页文字层提取价税合计（全文最大 ¥ 金额，不依赖 AI）。

    图片扫描件无文字层时返回 None。
    """
    text = page.extract_text() or ""
    nums = [Decimal(m.replace(",", "")) for m in AMOUNT_RE.findall(text)]
    return max(nums) if nums else None


def build(src: Path, out: Path, use_cache: bool = True, code: str = None):
    groups = discover(src, exclude={out.name})
    if not groups:
        sys.exit(f"目录 {src} 中没有找到 PDF 或图片")
    global SRC_DIR
    SRC_DIR = src

    pages, invoice_slots = [], []
    print("分组结果: " + ", ".join(f"{k}({len(v)}项)" for k, v in groups))

    # 1) 并发获取所有截图的裁剪坐标（缓存命中则不调接口）
    images = sorted({n for _, items in groups for n in items
                     if not n.lower().endswith(".pdf")})
    boxes = {}
    if images:
        cache = load_cache()  # --no-cache 时不读旧值；新结果仍写回，失败不丢旧值
        workers = min(ai_concurrency(), len(images))
        print(f"识别 {len(images)} 张截图裁剪区域（并发 {workers}）...")
        with ThreadPoolExecutor(max_workers=workers) as ex:
            futs = {n: ex.submit(crop_box_for, src / n, cache, use_cache)
                    for n in images}
            for n in images:  # 按提交顺序打印，输出稳定
                boxes[n], how = futs[n].result()
                print(f"  [{how}] {n}: 裁剪 {boxes[n][0]:.0f}% ~ {boxes[n][1]:.0f}%")
        save_cache(cache)

    # 2) 拼版合成：全局顺序两两配对（组内项相邻故组内优先成对；
    #    上一组落单的半栏由下一组的项补齐，发票不会因此另起新页）
    items = [n for _, its in groups for n in its]
    inv_readers = {}

    def load_inv(name):
        if name not in inv_readers:
            inv_readers[name] = PdfReader(src / name)
        return inv_readers[name].pages[0]

    for i in range(0, len(items), 2):
        chunk = items[i:i + 2]
        for slot, name in enumerate(chunk):
            if not name.lower().endswith(".pdf"):
                continue
            inv = load_inv(name)
            iw, ih = float(inv.mediabox.width), float(inv.mediabox.height)
            s = min((A4_W - 2 * MARGIN) / iw, (HALF_H - 2 * MARGIN) / ih)
            tx = (A4_W - iw * s) / 2
            ty = ((A4_H - HALF_H) if slot == 0 else 0) + (HALF_H - ih * s) / 2
            invoice_slots.append((len(pages) + 1, name, slot, s, tx, ty))
        pages.append(compose_page(chunk, boxes, code))

    print(f"共 {len(pages)} 页，开始合成 PDF ...")
    buf = io.BytesIO()
    pages[0].save(buf, "PDF", save_all=True, append_images=pages[1:],
                  resolution=DPI)
    buf.seek(0)

    reader = PdfReader(buf)
    writer = PdfWriter()
    for p in reader.pages:
        writer.add_page(p)

    for page_no, name, slot, s, tx, ty in invoice_slots:
        writer.pages[page_no - 1].merge_transformed_page(
            load_inv(name), Transformation().scale(s).translate(tx, ty))

    # 发票金额汇总（文字层提取，不用 AI）
    print("发票金额（价税合计）:")
    total = Decimal("0")
    for name in sorted(inv_readers):
        amt = extract_invoice_amount(inv_readers[name].pages[0])
        if amt is None:
            print(f"  {name}: 未提取到金额（可能是扫描图片型发票）")
        else:
            total += amt
            print(f"  {name}: ¥{amt:,.2f}")
    if total:
        print(f"发票合计: ¥{total:,.2f}")

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
    ap = argparse.ArgumentParser(
        description="发票 + 付款截图 自动拼版为 A4 PDF")
    ap.add_argument("src", nargs="?", default=str(HOME / "Downloads/fp"),
                    help="材料目录（默认 ~/Downloads/fp）")
    ap.add_argument("--no-cache", action="store_true",
                    help="忽略缓存，所有截图重新识别（新结果写回缓存）")
    ap.add_argument("--code",
                    help="编号（如 202609290007），印在每张发票右上角")
    args = ap.parse_args()

    src = Path(args.src).resolve()
    if not src.is_dir():
        sys.exit(f"目录不存在: {src}")
    out = HOME / "Downloads" / f"{src.name}_合并.pdf"
    build(src, out, use_cache=not args.no_cache, code=args.code)
    open_in_chrome(out)


if __name__ == "__main__":
    main()
