import os
import logging
from dotenv import load_dotenv
from flask import Flask, jsonify, request
from crawl_runner import LululemonCrawler

# 로깅 설정
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# .env 로드
basedir = os.path.abspath(os.path.dirname(__file__))
load_dotenv(os.path.join(basedir, '.env'))

# 크롤러 전역 생성
crawler = LululemonCrawler()

app = Flask(__name__)
API_TOKEN = os.getenv("API_TOKEN", "")

def is_authorized(req) -> bool:
    token = req.headers.get("X-API-KEY", "")
    return bool(API_TOKEN) and token == API_TOKEN

@app.post("/api/crawl")
def handle_crawl():
    if not is_authorized(request):
        logger.warning("권한 없는 접근 시도")
        return jsonify({"success": False, "message": "unauthorized"}), 401

    req_data = request.get_json()
    url = req_data.get('url')

    if not url:
        return jsonify({"success": False, "message": "URL is required"}), 400

    logger.info(f"크롤링 요청 시작: {url}")
    try:
        soup = crawler.product_api_data(url)

        if not soup:
            return jsonify({"success": False, "message": "Crawler failed, retry later"}), 500

        return jsonify({
            "success": True,
            "soup": str(soup)  # [핵심 수정] BeautifulSoup 객체를 문자열로 변환
        })
    except Exception as e:
        logger.error(f"API 핸들러 에러: {e}")
        return jsonify({"success": False, "message": str(e)}), 500

if __name__ == "__main__":
    # use_reloader=False 필수 (안 하면 브라우저가 계속 뜸)
    app.run(debug=True, use_reloader=False, port=5000)