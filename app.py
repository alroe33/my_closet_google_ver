from flask import Flask, render_template, request, jsonify, redirect, url_for, flash
from flask_login import LoginManager, UserMixin, login_user, login_required, logout_user, current_user
import os
import requests
import time
import pymysql
from werkzeug.security import generate_password_hash, check_password_hash
from PIL import Image
# ▼▼▼ 구글 AI 관련 임포트 ▼▼▼
import google.generativeai as genai
from vertexai.preview.vision_models import ImageGenerationModel
import vertexai
from google.oauth2 import service_account

app = Flask(__name__)
app.secret_key = 'super_secret_key'

# 현재 app.py가 있는 폴더의 경로를 알아냄
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
KEY_PATH = os.path.join(BASE_DIR, "google_key.json")

print(f"🔑 인증 키 경로: {KEY_PATH}") # 경로가 맞는지 터미널에 출력해봄


# ▼▼▼ [설정] MariaDB 연결 정보 (본인 환경에 맞게 수정!) ▼▼▼
db_config = {
    'host': 'localhost',
    'user': 'root',          # MariaDB 아이디 (보통 root)
    'password': 'abc123',  # MariaDB 비밀번호 (설치할 때 정한 것)
    'db': 'fashion_app',
    'charset': 'utf8'
}
# 구글 설정 (프로젝트 ID 입력 필수!)
PROJECT_ID = "gen-lang-client-0653881767"  # <-- 여기에 프로젝트 ID 입력
LOCATION = "us-central1"

try:
    my_credentials = service_account.Credentials.from_service_account_file(KEY_PATH)
    print("✅ 자격 증명(JSON) 로드 성공!")
except Exception as e:
    print(f"❌ 자격 증명 로드 실패 (파일을 확인하세요): {e}")

# 3. Vertex AI 초기화할 때 'credentials'를 직접 넣어주기 (환경 변수 무시)
vertexai.init(project=PROJECT_ID, location=LOCATION, credentials=my_credentials)

genai.configure(api_key="AIzaSyCIQXmwuo_ZmzcEoIdlTy3Gar4cV9d6o24") # Gemini API 키 입력

# Flask-Login 설정
login_manager = LoginManager()
login_manager.init_app(app)
login_manager.login_view = 'login' # 로그인 안 된 사용자가 접근하면 여기로 보냄

# 사용자 클래스 (세션 관리용)
class User(UserMixin):
    def __init__(self, id, username):
        self.id = id
        self.username = username

# 세션 로더 (로그인 유지 확인)
@login_manager.user_loader
def load_user(user_id):
    conn = pymysql.connect(**db_config)
    cur = conn.cursor()
    cur.execute("SELECT id, username FROM users WHERE id = %s", (user_id,))
    res = cur.fetchone()
    conn.close()
    if res:
        return User(id=res[0], username=res[1])
    return None

# 파일 저장 경로 설정
STATIC_FOLDER = 'static'
RESULT_FOLDER = os.path.join(STATIC_FOLDER, 'result')
TEMP_FOLDER = os.path.join(STATIC_FOLDER, 'temp')
os.makedirs(RESULT_FOLDER, exist_ok=True)
os.makedirs(TEMP_FOLDER, exist_ok=True)

# --- 라우트 (페이지) ---

@app.route('/')
@login_required # 로그인이 꼭 필요한 페이지
def home():
    return render_template('index.html', username=current_user.username)

@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        username = request.form['username']
        password = request.form['password']

        conn = pymysql.connect(**db_config)
        cur = conn.cursor()
        cur.execute("SELECT id, username, password FROM users WHERE username = %s", (username,))
        user_data = cur.fetchone()
        conn.close()

        if user_data and check_password_hash(user_data[2], password):
            user = User(id=user_data[0], username=user_data[1])
            login_user(user)
            return redirect(url_for('home'))
        else:
            flash('아이디 또는 비밀번호가 틀렸습니다.')
    
    return render_template('login.html')

@app.route('/register', methods=['GET', 'POST'])
def register():
    if request.method == 'POST':
        username = request.form['username']
        password = request.form['password']
        
        # 비밀번호 암호화 (보안 필수!)
        hashed_pw = generate_password_hash(password)

        try:
            conn = pymysql.connect(**db_config)
            cur = conn.cursor()
            cur.execute("INSERT INTO users (username, password) VALUES (%s, %s)", (username, hashed_pw))
            conn.commit()
            conn.close()
            flash('회원가입 성공! 로그인해주세요.')
            return redirect(url_for('login'))
        except pymysql.err.IntegrityError:
            flash('이미 존재하는 아이디입니다.')
        except Exception as e:
            flash(f'오류 발생: {e}')

    return render_template('register.html')

@app.route('/logout')
@login_required
def logout():
    logout_user()
    return redirect(url_for('login'))

# app.py 의 generate 함수를 이걸로 덮어씌우세요!

@app.route('/generate', methods=['POST'])
@login_required
def generate():
    print("🎨 [서버] 구글 AI(Gemini + Imagen) 생성 요청 시작!")

    try:
        # 1. 데이터 받기 및 저장
        model_file = request.files['model_image']
        top_url = request.form.get('top_url')
        bottom_url = request.form.get('bottom_url')
        
        # 내 사진 로컬 저장
        user_img_path = os.path.join(TEMP_FOLDER, f"user_{current_user.id}.jpg")
        model_file.save(user_img_path)
        
        # ▼▼▼ [수정 1] PIL 이미지로 바로 열기 (upload_file 안 씀) ▼▼▼
        user_img = Image.open(user_img_path)
        
        # Gemini에게 보낼 내용 리스트 (텍스트 + 이미지 객체)
        gemini_inputs = [user_img]
        clothes_desc = ""

        # 상의 처리
        if top_url and top_url != 'null':
            top_path = os.path.join(TEMP_FOLDER, f"top_{current_user.id}.jpg")
            with open(top_path, "wb") as f: f.write(requests.get(top_url).content)
            
            # 이미지 열어서 리스트에 추가
            top_img = Image.open(top_path)
            gemini_inputs.append(top_img)
            clothes_desc += " - Top: The user is wearing the selected Top image."
        
        # 하의 처리
        if bottom_url and bottom_url != 'null':
            bottom_path = os.path.join(TEMP_FOLDER, f"bottom_{current_user.id}.jpg")
            with open(bottom_path, "wb") as f: f.write(requests.get(bottom_url).content)
            
            # 이미지 열어서 리스트에 추가
            bottom_img = Image.open(bottom_path)
            gemini_inputs.append(bottom_img)
            clothes_desc += " - Bottom: The user is wearing the selected Bottom image."

        # 2. [Gemini] 프롬프트 엔지니어링
        gemini_model = genai.GenerativeModel('gemini-3-pro-preview')
        
        system_instruction = f"""
        You are a professional fashion photographer's assistant.
        Your goal is to describe 'Image 1' (User) so accurately that an AI painter can recreate their BODY SHAPE and HEIGHT exactly.

        Step 1. Analyze the User's BODY in extreme detail.
        - **Height:** Does the user look Tall, Short, or Average? (e.g., "Tall stature", "Petite")
        - **Build:** Skinny, Athletic, Curvy, Muscular, Broad shoulders? (e.g., "Slender build with long legs")
        - **Proportions:** Leg-to-torso ratio. (Important for preserving height)
        - **Pose:** Describe the exact standing pose.

        Step 2. Describe the Clothes provided in other images.
        {clothes_desc}
        
        Step 3. Create a prompt for Imagen 3.
        - Start with: "A full-body, low-angle fashion shot of..." (Low-angle makes people look taller)
        - Include the specific body descriptors from Step 1.
        - Ensure the background vibe is similar to Image 1.
        """
        
        # ▼▼▼ [수정 2] 텍스트 지시사항을 리스트 맨 앞에 추가하고 전송 ▼▼▼
        full_inputs = [system_instruction] + gemini_inputs
        
        print("🧠 [Gemini] 체형 및 비율 정밀 분석 중...")
        response = gemini_model.generate_content(full_inputs)
        generated_prompt = response.text
        print(f"📝 [프롬프트] {generated_prompt}")

        # 3. [Imagen] 이미지 생성 (9:16 비율)
        print("🎨 [Imagen] 전신(9:16) 이미지 생성 중...")
        imagen_model = ImageGenerationModel.from_pretrained("imagen-3.0-generate-001")
        
        images = imagen_model.generate_images(
            prompt=generated_prompt,
            number_of_images=1,
            aspect_ratio="9:16",
            person_generation="allow_adult",
            safety_filter_level="block_some"
        )

        # 4. 결과 저장
        output_filename = f"google_gen_{current_user.id}_{int(time.time())}.png"
        save_path = os.path.join(RESULT_FOLDER, output_filename)
        images[0].save(location=save_path, include_generation_parameters=False)
        
        return jsonify({'status': 'success', 'image_path': f"/{save_path.replace(os.sep, '/')}"})

    except Exception as e:
        print(f"❌ 에러: {e}")
        return jsonify({'status': 'error', 'message': str(e)})
    
if __name__ == '__main__':
    app.run(debug=True, port=5000)