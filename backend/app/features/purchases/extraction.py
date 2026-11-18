from dataclasses import dataclass
from datetime import datetime
from io import BytesIO
import logging
import os
from pathlib import Path
import re
import subprocess
import unicodedata

import pymupdf
from PIL import Image, ImageOps

logger = logging.getLogger(__name__)
MAX_PAGES = 12
MAX_PIXELS = 24_000_000


@dataclass
class Extraction:
    text: str
    method: str
    warning: str | None = None
    supplier_text: str = ""


def validate_file(kind, filename, content):
    suffix = Path(filename).suffix.lower()
    if kind == "receipt":
        expected = {".jpg": "JPEG", ".jpeg": "JPEG", ".png": "PNG"}.get(suffix)
        try:
            with Image.open(BytesIO(content)) as image:
                if not expected or image.format != expected or getattr(image, "n_frames", 1) != 1:
                    raise ValueError()
                if image.width * image.height > MAX_PIXELS:
                    raise ValueError("이미지 해상도가 너무 큽니다.")
                image.load()
        except (OSError, SyntaxError, Image.DecompressionBombError) as exc:
            raise ValueError("올바른 JPG 또는 PNG 이미지 한 장을 선택해 주세요.") from exc
        except ValueError as exc:
            raise ValueError(str(exc) or "올바른 JPG 또는 PNG 이미지 한 장을 선택해 주세요.") from exc
        return ("image/jpeg" if expected == "JPEG" else "image/png"), 1

    if suffix != ".pdf" or not content.startswith(b"%PDF-"):
        raise ValueError("올바른 PDF 파일을 선택해 주세요.")
    try:
        with pymupdf.open(stream=content, filetype="pdf") as pdf:
            if pdf.needs_pass or not 1 <= len(pdf) <= MAX_PAGES:
                raise ValueError(f"암호가 없는 {MAX_PAGES}쪽 이하 PDF를 선택해 주세요.")
            page_count = len(pdf)
    except (pymupdf.FileDataError, RuntimeError) as exc:
        raise ValueError("PDF 파일을 읽을 수 없습니다.") from exc
    return "application/pdf", page_count


def ocr(image_bytes):
    args = [os.getenv("TESSERACT_CMD", "tesseract"), "stdin", "stdout",
            "-l", os.getenv("TESSERACT_LANG", "kor+eng"), "--psm", "6"]
    if os.getenv("TESSDATA_PREFIX"):
        args.extend(["--tessdata-dir", os.environ["TESSDATA_PREFIX"]])
    result = subprocess.run(args, input=image_bytes, capture_output=True, timeout=18, check=True)
    return result.stdout.decode("utf-8", errors="replace").strip()


def render_page(page, max_side):
    scale = min(2.7, max_side / max(page.rect.width, page.rect.height))
    return page.get_pixmap(matrix=pymupdf.Matrix(scale, scale), colorspace=pymupdf.csGRAY).tobytes("png")


def pdf_preview(content, page_number):
    with pymupdf.open(stream=content, filetype="pdf") as pdf:
        if not 1 <= page_number <= len(pdf):
            raise IndexError(page_number)
        return render_page(pdf[page_number - 1], 1500)


def supplier_column(page):
    suppliers = page.search_for("공급자")
    recipients = page.search_for("공급받는 자") + page.search_for("공급받는자")
    headings = page.search_for("작성일자") + page.search_for("작성 일자")
    for supplier in suppliers:
        recipient = next((box for box in recipients if abs(box.y0 - supplier.y0) <= 10 and box.x0 > supplier.x1), None)
        if recipient is None:
            continue
        bottom = min((box.y0 for box in headings if box.y0 > supplier.y1), default=page.rect.y1)
        region = pymupdf.Rect(page.rect.x0, min(supplier.y0, recipient.y0), (supplier.x1 + recipient.x0) / 2, bottom)
        return page.get_text(clip=region, sort=True)
    return ""


def read_pdf(content):
    pages = []
    supplier_sections = []
    methods = set()
    failed = False
    with pymupdf.open(stream=content, filetype="pdf") as pdf:
        for page in pdf:
            text = page.get_text(sort=True).strip()
            if len(re.findall(r"[가-힣A-Za-z0-9]", text)) >= 12:
                methods.add("pdf_text")
                supplier_sections.append(supplier_column(page))
            else:
                methods.add("ocr")
                try:
                    text = ocr(render_page(page, 2400))
                except (OSError, subprocess.SubprocessError, RuntimeError) as exc:
                    logger.warning("PDF page OCR failed (%s)", type(exc).__name__)
                    failed = True
            pages.append(text)
    text = "\n\n".join(pages).strip()
    warning = "읽지 못한 페이지가 있습니다. 원본을 대조해 직접 입력해 주세요." if failed else None
    return Extraction(text, "+".join(sorted(methods)), warning, "\n".join(supplier_sections).strip())


def extract(kind, content):
    try:
        if kind == "document":
            result = read_pdf(content)
        else:
            with Image.open(BytesIO(content)) as image:
                gray = ImageOps.autocontrast(ImageOps.exif_transpose(image).convert("L"))
                gray.thumbnail((2600, 2600))
                output = BytesIO()
                gray.save(output, format="PNG")
            result = Extraction(ocr(output.getvalue()), "ocr")
        if not result.text and not result.warning:
            result.warning = "읽을 수 있는 텍스트가 없습니다. 원본을 보며 직접 입력해 주세요."
        result.text = result.text[:60000]
        return result
    except (OSError, subprocess.SubprocessError, RuntimeError, ValueError) as exc:
        logger.warning("Purchase extraction failed (%s)", type(exc).__name__)
        return Extraction("", "unavailable", "자동 추출에 실패했습니다. 원본을 보며 직접 입력해 주세요.")


def labeled(text, labels, value):
    match = re.search(rf"(?:^|\s)(?:{labels})[ \t]*[:：.]?[ \t]*({value})", text, re.MULTILINE)
    return match.group(1).strip() if match else ""


def date_value(text, labels, include_time=False):
    date = r"20\d{2}\s*[-./년]\s*\d{1,2}\s*[-./월]\s*\d{1,2}(?:\s*일)?"
    clock = r"\d{1,2}:\d{2}(?::\d{2})?"
    pattern = rf"{date}(?:[ T]+{clock})?" if include_time else date
    value = labeled(text, labels, pattern)
    if include_time and not value:
        match = re.search(rf"{date}[ T]+{clock}", text)
        value = match.group() if match else ""
    if not value:
        return ""
    parts = [int(part) for part in re.findall(r"\d+", value)]
    try:
        parsed = datetime(*parts)
    except ValueError:
        return ""
    return parsed.isoformat(timespec="minutes") if len(parts) > 3 else parsed.date().isoformat()


MONEY_VALUE = r"-?(?:\d{1,3}(?:,[ 	]*\d{3})+|\d{1,12})(?![\d,.])"


BUSINESS_LABEL = r"(?:사\s*업\s*자\s*)?등\s*록\s*번\s*호"
BUSINESS_NUMBER = r"(?<![0-9])[0-9]{3}[ \t]*[-‐‑–−]?[ \t]*[0-9]{2}[ \t]*[-‐‑–−]?[ \t]*[0-9]{5}(?![0-9])"


def supplier_business_number(text, supplier=""):
    explicit = labeled(text, rf"공\s*급\s*자\s*{BUSINESS_LABEL}", BUSINESS_NUMBER)
    paired = ""
    in_columns = False
    for line in text.splitlines():
        if re.fullmatch(r"\s*공\s*급\s*자[ \t|]+공\s*급\s*받\s*는\s*자\s*", line):
            in_columns = True
        if in_columns:
            if re.search(r"작\s*성\s*일\s*자", line):
                break
            numbers = re.findall(rf"{BUSINESS_LABEL}[ \t]*[:：.]?[ \t]*({BUSINESS_NUMBER})", line)
            if len(numbers) == 2:
                paired = numbers[0]
                break
    scoped = labeled(supplier, BUSINESS_LABEL, BUSINESS_NUMBER)
    candidates = re.findall(BUSINESS_NUMBER, supplier)
    value = explicit or paired or scoped or (candidates[0] if len(candidates) == 1 else "")
    digits = re.sub(r"[^0-9]", "", value)
    return f"{digits[:3]}-{digits[3:5]}-{digits[5:]}" if digits else ""


def amount(text, labels):
    value = labeled(text, labels, MONEY_VALUE)
    digits = re.sub(r"[, 	]", "", value)
    return int(digits) if digits and len(digits.lstrip("-")) <= 12 else None


def invoice_table_values(text):
    rows = [
        re.split(r"[ 	]{2,}|	|\|", line.strip())
        for line in text.splitlines() if line.strip()
    ]
    keys = {"작성일자", "공급가액", "세액", "합계금액", "품목"}
    found = {}
    for header_row, data_row in zip(rows, rows[1:]):
        headers = [re.sub(r"\s+", "", cell).strip(":") for cell in header_row]
        if len(headers) != len(data_row) or len(keys.intersection(headers)) < 2:
            continue
        for label, value in zip(headers, data_row):
            value = value.strip()
            if label == "작성일자" and date_value(f"{label}: {value}", label):
                found.setdefault(label, value)
            elif label in {"공급가액", "세액", "합계금액"} and re.fullmatch(rf"{MONEY_VALUE}[ 	]*원?", value):
                found.setdefault(label, value)
            elif label == "품목" and label != value and re.search(r"[가-힣A-Za-z]", value):
                found.setdefault(label, value)
    return "\n".join(f"{label}: {value}" for label, value in found.items())


def receipt_vendor(text):
    text = unicodedata.normalize("NFKC", text)
    name = labeled(text, r"가\s*맹\s*점\s*(?:명|상\s*호)|거\s*래\s*처\s*명|상\s*호", r"[^\n|]+")
    if name:
        return name

    # Receipts often print the merchant as a standalone line in the header.
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    headings = (
        r"(?:(?:시연용|가상|구매|카드|신용카드|현금|고객용|가맹점용)\s*)*"
        r"(?:영수증|매출\s*전표|결제\s*내역)(?:\s*\(?(?:고객용|가맹점용)\)?)?"
    )
    metadata = r"(?:사업자\s*(?:등록\s*)?번호|거래\s*일시|결제\s*일시|(?:카드\s*)?승인\s*번호)"
    end = next((index for index, line in enumerate(lines) if re.match(metadata, line)), None)
    if end is None or end > 8:
        return ""
    header = lines[:end]
    if not any(re.fullmatch(headings, line) for line in header):
        return ""
    names = [
        line for line in header
        if not re.fullmatch(headings, line)
        and len(line) <= 40
        and re.fullmatch(r"[\w .&()㈜·-]+", line)
        and re.search(r"[가-힣A-Za-z]", line)
    ]
    # Ignore short lowercase OCR fragments when the header contains a Korean name.
    if any(re.search(r"[가-힣]", name) for name in names):
        names = [name for name in names if not re.fullmatch(r"[a-z]{1,3}", name)]
    return names[0] if len(names) == 1 else ""


def parse_fields(kind, raw, supplier_text=""):
    text = unicodedata.normalize("NFKC", raw)
    money_labels = r"합\s*계\s*(?:금\s*액)?|총\s*금\s*액"
    if kind == "receipt":
        return "receipt", {
            "vendor": receipt_vendor(text),
            "transaction_date": date_value(text, r"거\s*래\s*(?:일\s*시|일\s*자)|결\s*제\s*일\s*시", True),
            "approval_number": labeled(text, r"(?:카\s*드\s*)?승\s*인\s*번\s*호", r"\d{6,12}\b"),
            "total_amount": amount(text, money_labels + r"|결\s*제\s*금\s*액|승\s*인\s*금\s*액"),
        }

    compact = re.sub(r"\s+", "", text)
    recognized = "세금계산서" in compact and "공급자" in compact and (
        "공급받는자" in compact or "공급가액" in compact
    )
    if not recognized:
        return "unknown", {}
    # Prefer explicitly qualified labels. Use a supplier-only section for generic labels.
    supplier_start = re.search(r"공\s*급\s*자", text)
    receiver_start = re.search(r"공\s*급\s*받\s*는\s*자", text)
    supplier = unicodedata.normalize("NFKC", supplier_text)
    if not supplier and supplier_start and receiver_start and receiver_start.start() > supplier_start.end():
        supplier = text[supplier_start.end():receiver_start.start()]
    values_text = text + "\n" + invoice_table_values(text)
    vendor = r"[^\n|]+?(?=[ \t]{2,}|[ \t]*\||$)"
    return "tax_invoice", {
        "vendor": labeled(text, r"공\s*급\s*자\s*상\s*호", vendor)
        or labeled(text, r"공\s*급\s*자[ \t]*[:：]", vendor)
        or labeled(supplier, r"상\s*호(?:\s*\(\s*법\s*인\s*명\s*\))?", vendor),
        "business_number": supplier_business_number(text, supplier),
        "transaction_date": date_value(values_text, r"작\s*성\s*일\s*자"),
        "approval_number": labeled(text, r"(?:세\s*금\s*계\s*산\s*서\s*)?승\s*인\s*번\s*호", r"\d{8}[- ]?\d{8}[- ]?\d{8}\b"),
        "item_summary": labeled(values_text, r"품\s*목[ \t]*(?:요\s*약|명|[:：])", r"[^\n|]+"),
        "supply_amount": amount(values_text, r"공\s*급\s*가\s*액"),
        "tax_amount": amount(values_text, r"세\s*액"),
        "total_amount": amount(values_text, money_labels),
    }
