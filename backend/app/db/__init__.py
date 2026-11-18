import os
from pathlib import Path

from sqlalchemy import create_engine
from sqlalchemy.orm import DeclarativeBase, sessionmaker

DATA_DIR = Path(__file__).resolve().parents[3] / "data"


class Base(DeclarativeBase):
    pass


def make_engine():
    url = os.getenv("DATABASE_URL")
    if not url:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        url = f"sqlite:///{(DATA_DIR / 'purchase-records.sqlite3').as_posix()}"
    options = {"pool_pre_ping": True}
    if url.startswith("mysql+pymysql:") and os.getenv("DATABASE_SSL_CA"):
        options["connect_args"] = {
            "ssl_ca": os.environ["DATABASE_SSL_CA"],
            "ssl_verify_cert": True,
            "ssl_verify_identity": True,
        }
    return create_engine(url, **options)


engine = make_engine()
Session = sessionmaker(engine, expire_on_commit=False)
