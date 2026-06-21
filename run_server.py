import os

from dotenv import load_dotenv
from waitress import serve


# 현재 파일이 있는 경로 기준으로 .env 로드
BASE_DIR = os.path.abspath(os.path.dirname(__file__))
load_dotenv(os.path.join(BASE_DIR, ".env"))


# .env를 먼저 불러온 후 Flask 앱 import
from main import app  # noqa: E402


if __name__ == "__main__":
    port = int(os.getenv("PORT", "5000"))
    threads = int(os.getenv("WAITRESS_THREADS", "8"))

    print(
        f"crawl-server starting on 0.0.0.0:{port} "
        f"| waitress threads={threads}"
    )

    serve(
        app,
        host="0.0.0.0",
        port=port,
        threads=threads,
    )