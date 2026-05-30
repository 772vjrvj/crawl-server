import os
from dotenv import load_dotenv
from flask import Flask, jsonify, request
from crawl_runner import LululemonCrawler

# .env 로드
basedir = os.path.abspath(os.path.dirname(__file__))
dotenv_path = os.path.join(basedir, '.env')
if os.path.exists(dotenv_path):
    load_dotenv(dotenv_path)

# 1. 크롤러 인스턴스 전역 생성 (서버 시작 시 1회)
crawler = LululemonCrawler()

app = Flask(__name__)
API_TOKEN = os.getenv("API_TOKEN", "")

def is_authorized(req) -> bool:
    token = req.headers.get("X-API-KEY", "")
    return bool(API_TOKEN) and token == API_TOKEN

@app.post("/api/crawl")
def handle_crawl():
    if not is_authorized(request):
        return jsonify({"success": False, "message": "unauthorized"}), 401

    req_data = request.get_json()
    url = req_data.get('url')
    if not url:
        return jsonify({"success": False, "message": "URL is required"}), 400

    try:
        # 가공 없이 원본 데이터 반환
        next_data, name = crawler.product_api_data(url)

        return jsonify({
            "success": True,
            "product_name": name,
            "next_data": next_data
        })
    except Exception as e:
        return jsonify({"success": False, "message": str(e)}), 500

if __name__ == "__main__":
    # use_reloader=False로 설정해야 서버 재시작 시 브라우저가 여러 개 뜨지 않습니다.
    app.run(debug=True, use_reloader=False, port=5000)