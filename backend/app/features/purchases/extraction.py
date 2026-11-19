from dataclasses import dataclass
from datetime import datetime
from io import BytesIO
import logging
import os
from pathlib import Path
import re
import shutil
import subprocess
import unicodedata

import pymupdf
from PIL import Image, ImageOps

logger = logging.getLogger(__name__)
MAX_PAGES = 12
MAX_PIXELS = 24_000_000
EXTRACTION_REVISION = 2
PROJECT_DATA = Path(__file__).resolve().parents[4] / "data"


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


def ocr_configuration():
    executable = os.getenv("TESSERACT_CMD") or shutil.which("tesseract")
    if not executable and os.name == "nt":
        installed = Path(os.getenv("ProgramFiles", r"C:\Program Files")) / "Tesseract-OCR" / "tesseract.exe"
        if installed.is_file():
            executable = str(installed)
    langs = os.getenv("TESSERACT_LANG", "kor+eng")
    tessdata = os.getenv("TESSDATA_PREFIX")
    if not tessdata:
        local_data = PROJECT_DATA / "tessdata"
        if all((local_data / f"{lang}.traineddata").is_file() for lang in langs.split("+")):
            tessdata = str(local_data)
    return executable or "tesseract", langs, tessdata


def ocr(image_bytes, page_mode=6):
    executable, langs, tessdata = ocr_configuration()
    args = [executable, "stdin", "stdout", "-l", langs, "--psm", str(page_mode)]
    if tessdata:
        args.extend(["--tessdata-dir", tessdata])
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


def read_pdf(content, kind):
    pages = []
    supplier_sections = []
    methods = set()
    failed = False
    with pymupdf.open(stream=content, filetype="pdf") as pdf:
        for page in pdf:
            text = page.get_text(sort=True).strip()
            compact = re.sub(r"\s+", "", text)
            has_fields = bool(re.search(r"세금계산서|거래명세[서표]|납품명세[서표]|공급자|등록번호|합계금액", compact))
            has_text = len(re.findall(r"[가-힣A-Za-z0-9]", text)) >= 12
            image_page = not has_fields and any(
                pymupdf.Rect(image["bbox"]).get_area() >= page.rect.get_area() * 0.3
                for image in page.get_image_info()
            )
            if has_text and not image_page:
                methods.add("pdf_text")
                supplier_sections.append(supplier_column(page))
            else:
                methods.add("ocr")
                try:
                    recognized = read_ocr(kind, render_page(page, 2600))
                    if recognized:
                        text = recognized
                    elif text:
                        methods.add("pdf_text")
                except (OSError, subprocess.SubprocessError, RuntimeError) as exc:
                    logger.warning("PDF page OCR failed (%s)", type(exc).__name__)
                    failed = True
                    if text:
                        methods.add("pdf_text")
            pages.append(text)
    text = "\n\n".join(pages).strip()
    warning = "읽지 못한 페이지가 있습니다. 원본을 대조해 직접 입력해 주세요." if failed else None
    return Extraction(text, "+".join(sorted(methods)), warning, "\n".join(supplier_sections).strip())


def extract(kind, content):
    try:
        if kind != "receipt":
            result = read_pdf(content, kind)
        else:
            with Image.open(BytesIO(content)) as image:
                gray = ImageOps.autocontrast(ImageOps.exif_transpose(image).convert("L"))
                gray.thumbnail((2600, 2600))
                output = BytesIO()
                gray.save(output, format="PNG")
            result = Extraction(read_ocr(kind, output.getvalue()), "ocr")
        if not result.text and not result.warning:
            result.warning = "읽을 수 있는 텍스트가 없습니다. 원본을 보며 직접 입력해 주세요."
        result.text = result.text[:60000]
        return result
    except (OSError, subprocess.SubprocessError, RuntimeError, ValueError) as exc:
        logger.warning("Purchase extraction failed (%s)", type(exc).__name__)
        warning = ("OCR 실행 파일 또는 언어 모델을 확인해 주세요." if isinstance(exc, (FileNotFoundError, subprocess.CalledProcessError))
                   else "자동 추출에 실패했습니다. 원본을 보며 직접 입력해 주세요.")
        return Extraction("", "unavailable", warning)


def labeled(text, labels, value, *, next_line=False):
    separator = r"[ \t]*(?:\r?\n[ \t]*)?" if next_line else r"[ \t]*"
    match = re.search(rf"(?:^|\s)(?:{labels})[ \t]*[:：.]?{separator}({value})", text, re.MULTILINE)
    return match.group(1).strip() if match else ""


def date_value(text, labels, include_time=False, *, next_line=False):
    date = r"20\d{2}\s*[-./년]\s*\d{1,2}\s*[-./월]\s*\d{1,2}(?:\s*일)?"
    clock = r"\d{1,2}\s*[:시]\s*\d{2}(?:\s*[:분]\s*\d{2})?(?:\s*[분초])?"
    pattern = rf"{date}(?:[ T]+{clock})?" if include_time else date
    value = labeled(text, labels, pattern, next_line=next_line)
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


MONEY_VALUE = r"-?(?:\d{1,3}(?:[ \t]*,[ \t]*\d{3})+|\d{1,12})(?![\d,.]|[ \t]*,)"


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
    value = labeled(text, labels, MONEY_VALUE) or labeled(
        text, labels, rf"{MONEY_VALUE}(?=[ \t]*원?[ \t]*\r?$)", next_line=True,
    )
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
        if len(headers) == 1 and len(data_row) == 1:
            headers = header_row[0].split()
            data_row = data_row[0].split()
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
    merchant_label = r"가[ \t]*맹[ \t]*점[ \t]*(?:명|상[ \t]*호)|거[ \t]*래[ \t]*처[ \t]*명|상[ \t]*호"
    merchant_value = r"[^\n\r|]+?(?=[ \t]{2,}(?:사업자|전화|대표|주소)|[ \t]*\||\r?$)"
    name = (labeled(text, rf"^[ \t]*(?:{merchant_label})", merchant_value, next_line=True)
            or labeled(text, rf"(?:{merchant_label})[ \t]*[:：]", merchant_value))
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


STATEMENT_COLUMNS = {
    "품목": "name", "품목명": "name", "품명": "name", "상품명": "name",
    "수량": "quantity", "단가": "unit_price", "공급단가": "unit_price",
    "금액": "amount", "공급가액": "amount", "합계금액": "amount",
    "규격": "spec", "단위": "unit", "번호": "number", "순번": "number", "NO": "number", "비고": "note",
}
STATEMENT_NUMBER_PATTERN = r"-?(?:[0-9]{1,3}(?:,[0-9]{3})+|[0-9]{1,12})(?:\.[0-9]{1,4})?"
STATEMENT_ITEM_LIMIT = 200


def statement_number(value, unit):
    match = re.fullmatch(rf"({STATEMENT_NUMBER_PATTERN})(?:\s*(?:{unit}))?", value.strip(), re.IGNORECASE)
    if not match:
        return None
    digits = match.group(1).replace(",", "")
    parsed = float(digits) if "." in digits else int(digits)
    return parsed if abs(parsed) <= 999_999_999_999 else None


def statement_items(text):
    items = []
    columns = []
    for raw in text.splitlines():
        line = raw.strip().strip("|").strip()
        if not line:
            continue
        if re.match(r"^(?:합\s*계|총\s*(?:계|액|금액)|소\s*계)(?=\s|:|금액|$)", line):
            columns = []
            continue
        cells = re.split(r"\s*\|\s*|\t+| {2,}", line)
        header_parts = cells if len(cells) > 1 else line.split()
        header = [STATEMENT_COLUMNS.get(re.sub(r"\s+", "", part).upper().strip(".:")) for part in header_parts]
        if {"name", "quantity", "unit_price"} <= set(header):
            columns = header
            continue
        if not columns:
            continue

        if len(cells) != len(columns) and columns in (["name", "quantity", "unit_price"], ["name", "quantity", "unit_price", "amount"]):
            cells = line.rsplit(maxsplit=len(columns) - 1)
        if len(cells) != len(columns):
            continue
        values = dict(zip(columns, cells))
        name = values["name"].strip()
        quantity = statement_number(values["quantity"], r"개|EA|PCS|SET|매|박스")
        price = statement_number(values["unit_price"], r"원|KRW")
        if name and len(name) <= 200 and (quantity is not None or price is not None):
            items.append({"name": name, "quantity": quantity, "unit_price": price})
            if len(items) >= STATEMENT_ITEM_LIMIT:
                break
    return items


def statement_delivery_date(text):
    text = unicodedata.normalize("NFKC", text)
    delivery_label = r"(?:납\s*품|인\s*도)\s*일(?:\s*자|\s*시)?(?![가-힣])"
    text = re.sub(r"[‐‑–−]", "-", text)
    inline_text = text.replace("|", " ")
    inline_date = date_value(inline_text, delivery_label)
    if inline_date:
        return inline_date

    rows = [line.strip().strip("|") for line in text.splitlines() if line.strip().strip("|")]
    for header, row in zip(rows, rows[1:]):
        headers = re.split(r"\s*\|\s*|\t+| {2,}", header.strip())
        cells = re.split(r"\s*\|\s*|\t+| {2,}", row.strip())
        if len(headers) != len(cells):
            continue
        for label, value in zip(headers, cells):
            if re.fullmatch(delivery_label, label.strip(" :：")):
                delivery_date = date_value(f"납품일: {value}", "납품일")
                if delivery_date:
                    return delivery_date
    return date_value(inline_text, delivery_label, next_line=True) or date_value(
        inline_text, r"작\s*성\s*(?:일\s*자|일)|거\s*래\s*일\s*자|발\s*행\s*일\s*자",
        next_line=True,
    )


def statement_vendor(text, supplier_text=""):
    text = unicodedata.normalize("NFKC", text)
    name = r"[^\n|]+?(?=[ \t]{2,}|[ \t]*\||$)"
    company_label = r"상[ \t]*호(?:[ \t]*\([ \t]*법[ \t]*인[ \t]*명[ \t]*\))?"
    supplier_label = r"공[ \t]*급[ \t]*자"
    recipient_label = r"공[ \t]*급[ \t]*받[ \t]*는[ \t]*자"
    explicit = labeled(text, rf"{supplier_label}[ \t]*{company_label}|거[ \t]*래[ \t]*처[ \t]*명", name)
    if explicit:
        return explicit

    paired = paired_supplier_name(text, supplier_label, recipient_label, company_label)
    if paired:
        return paired
    supplier = re.search(rf"{supplier_label}(.*?){recipient_label}", text, re.DOTALL)
    return (
        labeled(supplier.group(1) if supplier else "", company_label, name)
        or labeled(text, rf"{supplier_label}[ \t]*[:：]", name)
        or labeled(supplier_text, company_label, name)
    )


def paired_supplier_name(text, supplier_label, recipient_label, company_label):
    paired = re.search(rf"(?m)^[ \t]*{supplier_label}[ \t|]+{recipient_label}[ \t|]*\r?$", text)
    if paired:
        for line in text[paired.end():].splitlines()[:12]:
            match = re.match(
                rf"^[ \t|]*{company_label}[ \t]*[:：|]?[ \t]*(.+?)[ \t|]+{company_label}(?=[ \t:：|])",
                line,
            )
            if match:
                return match.group(1).strip()
    return ""


def parse_statement(text, supplier_text, money_labels):
    if not re.search(r"(?:거\s*래|납\s*품)\s*명\s*세\s*[서표]", text):
        return "unknown", {}
    return "trade_statement", {
        "vendor": statement_vendor(text, supplier_text),
        "business_number": supplier_business_number(text, supplier_text),
        "transaction_date": statement_delivery_date(text),
        "statement_number": labeled(text, r"(?:거\s*래\s*)?명\s*세\s*(?:서\s*)?번\s*호|문\s*서\s*번\s*호", r"[A-Za-z0-9][A-Za-z0-9._/-]{0,63}"),
        "total_amount": amount(text, money_labels),
        **statement_tax_amounts(text),
        "items": statement_items(text),
    }


def statement_tax_amounts(text):
    tax_labels = r"부\s*가\s*(?:가\s*치\s*)?세|세\s*액"
    return {
        "supply_amount": amount(text, r"공\s*급\s*가\s*액|공\s*급\s*금\s*액|공\s*급\s*가"),
        "tax_amount": amount(text, tax_labels),
    }


def parse_fields(kind, raw, supplier_text=""):
    text = unicodedata.normalize("NFKC", raw)
    text = re.sub(r"[‐‑–−]", "-", text)
    money_labels = r"합\s*계\s*(?:금\s*액)?|총\s*금\s*액"
    if kind == "statement":
        return parse_statement(text, unicodedata.normalize("NFKC", supplier_text), money_labels)
    if kind == "receipt":
        return "receipt", {
            "vendor": receipt_vendor(text),
            "transaction_date": date_value(text, r"(?:거\s*래|결\s*제|승\s*인|구\s*매)\s*일\s*[시자]", True,
                                           next_line=True),
            "approval_number": labeled(text, r"(?:카\s*드\s*)?승\s*인\s*번\s*호", r"\d{6,12}\b", next_line=True),
            "total_amount": receipt_total(text, money_labels),
        }

    compact = re.sub(r"\s+", "", text)
    approval = labeled(text, r"(?:세\s*금\s*계\s*산\s*서\s*)?승\s*인\s*번\s*호",
                       r"\d{8}[- ]?\d{8}[- ]?\d{8}\b", next_line=True)
    has_invoice_fields = "공급가액" in compact and ("세액" in compact or "부가세" in compact)
    recognized = ("세금계산서" in compact and ("공급자" in compact or approval) and
                  ("공급받는자" in compact or "공급가액" in compact or approval)) or bool(
        approval and has_invoice_fields and "작성일자" in compact
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
    company_label = r"상[ \t]*호(?:[ \t]*\([ \t]*법[ \t]*인[ \t]*명[ \t]*\))?"
    return "tax_invoice", {
        "vendor": labeled(text, rf"공\s*급\s*자\s*{company_label}", vendor)
        or labeled(text, r"공\s*급\s*자[ \t]*[:：]", vendor)
        or paired_supplier_name(text, r"공[ \t]*급[ \t]*자", r"공[ \t]*급[ \t]*받[ \t]*는[ \t]*자", company_label)
        or labeled(supplier, company_label, vendor),
        "business_number": supplier_business_number(text, supplier),
        "transaction_date": date_value(values_text, r"작\s*성\s*일(?:\s*자)?|발\s*행\s*일\s*자", next_line=True),
        "approval_number": approval,
        "item_summary": labeled(values_text, r"품\s*목[ \t]*(?:요\s*약|명|[:：])", r"[^\n|]+"),
        "supply_amount": amount(values_text, r"공\s*급\s*가\s*액"),
        "tax_amount": amount(values_text, r"세\s*액|부\s*가\s*세"),
        "total_amount": amount(values_text, money_labels),
    }


def receipt_total(text, money_labels):
    paid = amount(text, r"(?:결\s*제|승\s*인|청\s*구|받\s*을)\s*금\s*액")
    return paid if paid is not None else amount(text, money_labels + r"|총\s*액")


def recognize_upload(kind, extraction):
    doc_type, fields = parse_fields(kind, extraction.text, extraction.supplier_text)
    if kind != "receipt" and doc_type == "unknown":
        other_kind = "statement" if kind == "document" else "document"
        other_type, other_fields = parse_fields(other_kind, extraction.text, extraction.supplier_text)
        if other_type != "unknown":
            return other_kind, other_type, other_fields
    return kind, doc_type, fields


def read_ocr(kind, image_bytes):
    text = ocr(image_bytes)
    score, expected = recognition_score(kind, text)
    if score == expected:
        return text
    try:
        sparse_text = ocr(image_bytes, page_mode=11)
    except (OSError, subprocess.SubprocessError, RuntimeError):
        return text
    return sparse_text if recognition_score(kind, sparse_text)[0] > score else text


def recognition_score(kind, text):
    _, document_type, fields = recognize_upload(kind, Extraction(text, "ocr"))
    if document_type == "unknown":
        return 0, 1
    expected = ["vendor", "transaction_date", "total_amount"]
    if document_type == "trade_statement":
        expected.append("items")
    else:
        expected.append("approval_number")
    if document_type == "tax_invoice":
        expected.extend(("business_number", "supply_amount", "tax_amount"))
    return sum(fields.get(field) not in (None, "", []) for field in expected), len(expected)
