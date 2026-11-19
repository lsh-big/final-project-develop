# Final Project

## 역할

| 담당 | 역할 | 기술 |
| --- | --- | --- |
| 프런트엔드 1명 | 전체 화면, 대시보드, API 연결 | React, Tailwind, Recharts |
| 성근 | 뉴스·SNS 수집, AI 문서 분류, TOP 10, ETL | Python, FastAPI, Airflow, Polars, Scikit-learn |
| 종범 | 주식·코인 수집 및 분석 | Python, FastAPI, Pandas, 금융 API |
| 승호 | 영수증 OCR 및 자동 등록 | Python, FastAPI, OCR, Playwright |
| 공동 | DB, API 통합, 테스트, 배포 | TiDB, SQL, GitHub, Render, EC2 |

## 실행

프런트엔드: frontend 폴더에서 npm install 후 npm run dev.
PowerShell에서 npm.ps1 실행이 차단되면 npm.cmd install / npm.cmd run dev 사용.

백엔드(Python 설치 필요): backend 폴더에서 다음 실행.

    python -m venv .venv
    .venv\Scripts\activate
    pip install -r requirements.txt
    uvicorn app.main:app --reload --port 8001

Docker 백엔드: docker compose up --build
API 문서: http://127.0.0.1:8001/docs

## 구조와 구현 범위

- frontend/: React 시작점 및 기존 화면(public/legacy)
- backend/app/features/: trends(성근), finance(종범), receipts(승호)
- backend/app/core, db, models, schemas: 공통 설정·DB·모델·스키마
- pipelines/: Airflow DAG, ETL 작업, 테스트(별도 Python 환경)
- models/, data/: 로컬 모델·데이터 저장 위치
- migrations/: Alembic 초기화 및 DB 변경 파일을 추가할 위치

### 구매 증빙 대조와 DB 적용

백엔드를 시작하면 기존 DB에 `purchase_links`와 `purchase_file_claims` 테이블을 추가합니다. 기존 구매 증빙 레코드는 그대로 사용할 수 있습니다. 배포 전에 적용하려면 기존 `DATABASE_URL`을 설정한 환경에서 `backend` 디렉터리로 이동하여 아래 명령을 실행합니다. DB 계정에는 테이블 생성 권한이 필요합니다.

```powershell
python -c "from app.features.purchases.router import init_db; init_db()"
```

**AI 문서**에서 매입 세금계산서와 거래명세서를 불러온 뒤, 상세의 **증빙 대조**에서 후보와 금액을 확인하고 **연결 확정** 또는 **연결 해제**를 누릅니다. 후보 날짜 범위는 `backend/app/features/purchases/matching.py`의 `MATCH_DATE_WINDOW_DAYS`에서 조정합니다. 동일 파일이나 승인번호는 **기존 증빙 보기**로 열 수 있으며, 거래처·날짜·금액만 일치하면 별도 거래인지 확인한 뒤 등록할 수 있습니다.

기본 SQLite DB를 사용하는 개인 PC 개발 서버에서 인증 연동 없이 조회하려면 `backend`에서 서버를 시작할 PowerShell에 **매입 단가 이력**은 `$env:AUTOFLOW_LOCAL_PRICE_HISTORY = '1'`, **미처리 증빙함**은 `$env:AUTOFLOW_LOCAL_PURCHASE_INBOX = '1'`을 설정합니다. `python -m uvicorn app.main:app --host 127.0.0.1 --port 8001 --no-proxy-headers`로 실행합니다. 확인 메모와 공유 서버에서는 사용자 및 증빙 접근 권한을 확인하는 기존 서버 연동을 사용합니다.
- docs/: planning, design, implementation, deployment
- .github/workflows/: 프런트엔드 빌드 및 Python 구문 검사 CI

백엔드는 구매 증빙 처리 API와 health 및 기능별 status API를 제공합니다. 구매 증빙의 DB와 OCR 설정은 아래 실행 방법을 참고하세요.

기존 HTML과 자산은 상대 경로를 유지한 채 frontend/public/legacy로 옮겼습니다. React의 대시보드 페이지는 iframe으로 기존 화면을 표시합니다. 트렌드·금융·운영 콘솔에서는 데모 데이터를 사용합니다. 기존 업로드 화면은 하단 링크에서 이용할 수 있습니다.
기존 프런트엔드 문서는 docs/implementation/legacy-frontend.md에 있습니다.

## 구매 증빙 처리 (Windows PowerShell)

**AI 문서** 메뉴에서 **매입 세금계산서**와 **거래명세서** 탭에는 PDF를, **구매 영수증** 탭에는 JPG·JPEG·PNG를 업로드합니다. 여러 파일을 한 번에 선택하면 파일별 처리 결과를 확인할 수 있습니다. 목록에서 각 파일을 열고 원본과 추출값을 비교한 뒤 개별적으로 **임시저장**하거나 **승인 및 저장**합니다. 상단의 **처리 이력 열기**를 누르면 별도 브라우저 창에서 세 문서 유형을 함께 조회합니다. 등록 기간(KST), 거래처, 문서 유형, 처리 상태로 필터링할 수 있으며 **엑셀 다운로드**는 적용된 필터를 사용합니다.

거래명세서는 거래처와 작성일자, 품목별 품목명·수량·단가를 확인합니다. 원본에 명세서 번호나 합계금액이 있으면 함께 입력할 수 있습니다. 엑셀의 **거래명세서 품목** 시트에서 품목별 수량과 단가를 숫자 셀로 확인할 수 있습니다.

메뉴의 숫자는 모든 증빙 유형의 검토 대기 건수를 합한 값입니다. **검토 대기함**을 누르면 해당 기록만 조회합니다. **임시저장**으로 빈 항목이 있는 상태에서도 수정값을 저장할 수 있으며, 대기함에서 기록을 다시 열어 검토를 이어갈 수 있습니다.

처리 이력의 **등록일시** 오른쪽 **삭제** 버튼으로 검토 대기 건과 승인 완료 건을 휴지통에 보낼 수 있습니다. **휴지통**에서 기록을 열어 **복원**하면 원본과 저장된 값이 유지되고 이전 처리 상태로 돌아갑니다. 휴지통 기록은 일반 이력과 엑셀에서 제외되며, 같은 파일을 다시 올리면 기존 기록을 열어 복원할 수 있습니다.

### 휴대폰에서 QR로 영수증 보내기

1. PC와 휴대폰을 같은 사내 Wi-Fi에 연결하고 PC에서 `ipconfig`로 해당 네트워크 어댑터의 IPv4 주소를 확인합니다. 아래 예시의 `192.168.1.10`을 그 주소로 바꿉니다.
2. PC 백엔드를 실행할 PowerShell에 `$env:RECEIPT_MOBILE_URL = 'http://192.168.1.10:8010'`을 설정하고 백엔드를 시작하거나 재시작합니다.
3. 다른 PowerShell에서 `backend` 폴더로 이동한 뒤, PC 백엔드와 동일한 `DATABASE_URL` 및 OCR 환경변수로 아래 휴대폰 업로드 서버를 실행합니다. 기본 SQLite를 쓰는 경우 두 셸에서 `DATABASE_URL`을 생략해도 됩니다.

```powershell
.\.venv\Scripts\python.exe -m uvicorn app.features.purchases.mobile:mobile_app --host 192.168.1.10 --port 8010
```

PC의 **AI 문서 → 구매 영수증 → 휴대폰으로 올리기**에서 QR을 스캔합니다. 갤러리에서 여러 장을 고르거나 카메라로 차례로 찍고 **PC로 전송**을 누릅니다. QR 하나로 최대 20장(파일당 10 MB)을 보낼 수 있습니다. PC에서 사진별 결과를 확인하고 각각 검토하거나, 검토 대기함에서 다시 열 수 있습니다. QR은 5분 동안 유효하며 한 장이라도 처리에 성공하면 소진됩니다.

휴대폰 서버는 영수증 사진 전송 화면만 제공하며 PC 백엔드와 Vite는 기존 루프백 주소를 사용합니다. 예시의 HTTP 주소는 신뢰하는 내부 네트워크에서 사용합니다. 휴대폰에서 접속이 차단되면 사내 Wi-Fi의 기기 간 연결 정책과 Windows 방화벽의 TCP 8010 인바운드 정책을 확인합니다. HTTPS를 제공하는 내부 프록시가 있다면 해당 접속 주소를 `RECEIPT_MOBILE_URL`에 설정합니다.

### Tesseract 설치와 환경변수

1. Windows용 Tesseract를 설치합니다. 예를 들어 PowerShell에서 `winget install --id UB-Mannheim.TesseractOCR --exact`를 실행합니다.
2. 설치 시 Korean 언어 데이터를 선택하거나 [Tesseract 한국어 모델](https://github.com/tesseract-ocr/tessdata_fast/blob/main/kor.traineddata)을 내려받습니다. `kor.traineddata`와 `eng.traineddata`를 같은 `tessdata` 폴더에 둡니다.
3. 저장소 루트에서 백엔드를 시작할 PowerShell에 환경변수를 설정합니다. 아래 경로는 설치 위치에 맞게 조정합니다.

```powershell
$env:TESSERACT_CMD = 'C:\Program Files\Tesseract-OCR\tesseract.exe'
$env:TESSDATA_PREFIX = 'C:\Program Files\Tesseract-OCR\tessdata'
$env:TESSERACT_LANG = 'kor+eng'
& $env:TESSERACT_CMD --tessdata-dir $env:TESSDATA_PREFIX --list-langs
```

PDF에 텍스트가 있으면 PyMuPDF로 읽고, 이미지로 된 페이지는 Tesseract로 읽습니다. 문서 유형과 필드는 추출된 텍스트의 레이블과 패턴으로 판별합니다. 외부 AI 모델과 API 키 없이 실행하며, 추출 실패 시 원본 미리보기와 직접 입력을 이용할 수 있습니다. 형식에 따라 자동 추출 결과가 달라질 수 있으므로 승인 전에 원본과 대조합니다.

### DB 초기화 및 실행

`DATABASE_URL`을 생략하면 저장소의 `data/purchase-records.sqlite3`를 사용합니다. 별도 DB로 시연할 경우 백엔드를 시작할 PowerShell에서 `$env:DATABASE_URL = 'sqlite:///C:/git/final-project-develop/data/purchase-demo.sqlite3'`를 설정합니다. 검증용 가상 영수증과 세금계산서는 이 별도 데모 DB에서만 등록합니다. MySQL·TiDB 연결을 사용할 경우 `DATABASE_URL`에 `mysql+pymysql://...` URL을 설정하고, CA 검증에는 `DATABASE_SSL_CA`에 인증서 파일 경로를 지정합니다. 연결 대상 DB는 먼저 생성합니다.

```powershell
Set-Location backend
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe -m app.features.purchases.router
.\.venv\Scripts\python.exe -m uvicorn app.main:app --reload --port 8001
```

초기화 명령과 서버 시작은 모두 구매 증빙 테이블이 없으면 생성합니다. 원본과 기록은 같은 DB에 보관됩니다. 다른 PowerShell에서 저장소의 `frontend`로 이동한 후 `npm.cmd install`과 `npm.cmd run dev`를 실행하고 Vite가 표시하는 로컬 주소를 엽니다. 기본 API 프록시는 `http://127.0.0.1:8001`이며, 포트를 바꾼 경우 프런트엔드 프로세스의 `DEV_API_TARGET`을 설정합니다.

업로드는 파일당 최대 10 MB이고 PDF는 최대 12쪽입니다. 파일 해시가 같으면 기존 기록을 엽니다. 영수증은 거래처와 거래일이 같은 카드 승인번호의 재등록을 막고, 세금계산서는 하이픈과 공백을 제외한 승인번호로 중복 여부를 확인합니다. 구매 증빙 화면은 로컬 실행을 기준으로 하며, 공유 환경에서는 접근 제어가 적용된 네트워크에서 운영해야 합니다.
