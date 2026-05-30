import os
from dotenv import load_dotenv
from flask import Flask, jsonify, request
from crawl_runner import LululemonCrawler  # 크롤러 클래스 임포트

# 1. .env 파일 경로 설정
basedir = os.path.abspath(os.path.dirname(__file__))
dotenv_path = os.path.join(basedir, '.env')

if os.path.exists(dotenv_path):
    load_dotenv(dotenv_path)
    print(f"[DEBUG] .env 파일을 로드했습니다: {dotenv_path}")
else:
    print(f"[ERROR] .env 파일을 찾을 수 없습니다!")

# 2. 크롤러 상주 인스턴스 생성 (서버 시작 시 딱 한 번 실행됨)
print("[DEBUG] 크롤러를 초기화 중입니다 (브라우저가 뜹니다)...")
crawler = LululemonCrawler()
print("[DEBUG] 크롤러 초기화 완료!")

app = Flask(__name__)

# 3. 인증 설정
API_TOKEN = os.getenv("API_TOKEN", "")

def is_authorized(req) -> bool:
    token = req.headers.get("X-API-KEY", "")
    return bool(API_TOKEN) and token == API_TOKEN

@app.get("/")
def index():
    return jsonify({"message": "crawl-server up", "status": "ok"})

@app.post("/api/crawl")
def handle_crawl():
    # 1. 인증 확인
    if not is_authorized(request):
        return jsonify({"success": False, "message": "unauthorized"}), 401

    # 2. URL 수신
    req_data = request.get_json()
    url = req_data.get('url')
    if not url:
        return jsonify({"success": False, "message": "URL is required"}), 400

    # 3. 미리 띄워둔 크롤러 인스턴스 사용
    try:
        options, name = crawler.product_api_data(url)

        return jsonify({
            "success": True,
            "product_name": name,
            "options": options
        })
    except Exception as e:
        return jsonify({"success": False, "message": str(e)}), 500

if __name__ == "__main__":
    # debug=True 모드에서는 코드가 변경될 때 서버가 재시작되어 브라우저가 두 번 뜰 수 있습니다.
    # 운영 환경에서는 debug=False로 설정하세요.
    app.run(debug=True, use_reloader=False)