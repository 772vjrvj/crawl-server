import os

from dotenv import load_dotenv
from waitress import serve

load_dotenv()

from main import app

if __name__ == "__main__":
    port = int(os.getenv("PORT", "5000"))
    print(f"crawl-server starting on 0.0.0.0:{port}")
    serve(
        app,
        host="0.0.0.0",
        port=port,
        threads=8
    )