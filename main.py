import os
import re
from datetime import datetime
from typing import List

from fastapi import FastAPI, BackgroundTasks
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse
from pydantic import BaseModel
from bs4 import BeautifulSoup
import uvicorn

# Thư viện AI mới của Google
from google import genai

# Thư viện hỗ trợ môi trường và Database
from dotenv import load_dotenv
from sqlalchemy import create_engine, Column, Integer, String, Text, DateTime, ForeignKey
from sqlalchemy.orm import declarative_base, sessionmaker, relationship

# ==========================================
# 1. KHỞI TẠO BIẾN MÔI TRƯỜNG & KẾT NỐI AIVEN
# ==========================================
load_dotenv()

# Lấy URL từ .env
raw_db_url = os.getenv("DATABASE_URL", "")

# Tự động sửa lỗi nếu link Aiven thiếu "pymysql"
if raw_db_url.startswith("mysql://"):
    DATABASE_URL = raw_db_url.replace("mysql://", "mysql+pymysql://", 1)
else:
    DATABASE_URL = raw_db_url

# Đường dẫn file chứng chỉ SSL (Tải từ Aiven Console và để cùng thư mục main.py)
CA_PEM_PATH = os.path.join(os.path.dirname(__file__), "ca.pem")

# Cấu hình Engine đặc biệt cho Aiven (Bắt buộc SSL)
engine = create_engine(
    DATABASE_URL,
    connect_args={
        "ssl": {
            "ca": CA_PEM_PATH
        }
    },
    pool_recycle=3600, # Tự động làm mới kết nối sau 1 giờ
    pool_pre_ping=True # Kiểm tra kết nối trước khi sử dụng để tránh lỗi "Lost connection"
)

SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()

# ==========================================
# 2. ĐỊNH NGHĨA CẤU TRÚC DATABASE (MODELS)
# ==========================================
class ClassRecord(Base):
    __tablename__ = "classes"
    id = Column(Integer, primary_key=True, index=True)
    class_code = Column(String(50), unique=True, index=True) 
    students = relationship("Student", back_populates="class_ref")
    lessons = relationship("Lesson", back_populates="class_ref")

class Student(Base):
    __tablename__ = "students"
    id = Column(Integer, primary_key=True, index=True)
    name = Column(String(100), index=True)
    class_id = Column(Integer, ForeignKey("classes.id")) 
    class_ref = relationship("ClassRecord", back_populates="students")
    evaluations = relationship("Evaluation", back_populates="student_ref")

class Lesson(Base):
    __tablename__ = "lessons"
    id = Column(Integer, primary_key=True, index=True)
    lesson_number = Column(Integer)
    class_id = Column(Integer, ForeignKey("classes.id")) 
    class_ref = relationship("ClassRecord", back_populates="lessons")
    evaluations = relationship("Evaluation", back_populates="lesson_ref")

class Evaluation(Base):
    __tablename__ = "evaluations"
    id = Column(Integer, primary_key=True, index=True)
    student_id = Column(Integer, ForeignKey("students.id")) 
    lesson_id = Column(Integer, ForeignKey("lessons.id"))   
    keywords = Column(Text)
    scores = Column(String(100))  
    ai_comment = Column(Text)
    created_at = Column(DateTime, default=datetime.utcnow)
    
    student_ref = relationship("Student", back_populates="evaluations")
    lesson_ref = relationship("Lesson", back_populates="evaluations")

# Tự động khởi tạo bảng trên Aiven Cloud
Base.metadata.create_all(bind=engine)

# ==========================================
# 3. LOGIC XỬ LÝ & BÓC TÁCH HTML
# ==========================================
app = FastAPI(title="LMS Performance Tracker API")
app.add_middleware(
    CORSMiddleware, allow_origins=["*"], allow_credentials=True, 
    allow_methods=["*"], allow_headers=["*"]
)

SYSTEM_LOGS = []
def add_log(msg: str, status: str = "INFO"):
    time_str = datetime.now().strftime("%H:%M:%S")
    if len(SYSTEM_LOGS) > 30: SYSTEM_LOGS.pop()
    SYSTEM_LOGS.insert(0, {"time": time_str, "msg": msg, "status": status})
    print(f"[{time_str}] [{status}] {msg}") 

class AIRequest(BaseModel):
    prompt: str
    model: str
    api_key: str
    keywords: str      
    scores: str        
    raw_html: str

def process_and_save_data(raw_html: str, keywords: str, scores_str: str, ai_comment: str):
    add_log("⚙️ Đang thực hiện bóc tách và đối chiếu dữ liệu...", "INFO")
    db = SessionLocal()
    try:
        soup = BeautifulSoup(raw_html, 'html.parser')
        
        # 1. Tìm Mã lớp (Lấy từ h6 Typography)
        class_code = "UNKNOWN"
        h6_tags = soup.find_all('h6', class_='MuiTypography-h6')
        for tag in h6_tags:
            match = re.search(r'\b([A-Z]{2,4}-[A-Z0-9]+-[A-Z0-9]+)\b', tag.text)
            if match:
                class_code = match.group(1)
                break
        
        # 2. Tìm Buổi học (# số)
        lesson_number = 1
        info_divs = soup.select('.info-container div')
        for div in info_divs:
            match = re.search(r'#\s*(\d+)', div.text)
            if match:
                lesson_number = int(match.group(1))
                break

        # 3. Thuật toán Đối chiếu chéo Tên (H3 trong Popup vs span danh sách)
        student_name = "Ẩn danh"
        valid_names = [span.get_text(strip=True) for span in soup.find_all('span', class_='name-display')]
        
        popup = soup.find('div', role='dialog') or soup
        h3_tags = popup.find_all('h3')
        for h3 in h3_tags:
            h3_text = h3.get_text(strip=True)
            for name in valid_names:
                if name in h3_text:
                    student_name = name
                    break
            if student_name != "Ẩn danh": break

        # 4. Lưu vào Aiven MySQL
        db_class = db.query(ClassRecord).filter(ClassRecord.class_code == class_code).first()
        if not db_class:
            db_class = ClassRecord(class_code=class_code)
            db.add(db_class); db.commit(); db.refresh(db_class)

        db_student = db.query(Student).filter(Student.name == student_name, Student.class_id == db_class.id).first()
        if not db_student:
            db_student = Student(name=student_name, class_id=db_class.id)
            db.add(db_student); db.commit(); db.refresh(db_student)

        db_lesson = db.query(Lesson).filter(Lesson.lesson_number == lesson_number, Lesson.class_id == db_class.id).first()
        if not db_lesson:
            db_lesson = Lesson(lesson_number=lesson_number, class_id=db_class.id)
            db.add(db_lesson); db.commit(); db.refresh(db_lesson)

        new_eval = Evaluation(
            student_id=db_student.id, lesson_id=db_lesson.id,
            keywords=keywords, scores=scores_str, ai_comment=ai_comment
        )
        db.add(new_eval); db.commit()
        add_log(f"💾 Dataset thành công: {student_name} | Lớp {class_code} | Buổi {lesson_number}", "SUCCESS")
        
    except Exception as e:
        db.rollback()
        add_log(f"❌ Lỗi lưu Database: {str(e)}", "ERROR")
    finally:
        db.close()

# ==========================================
# 4. API ENDPOINTS
# ==========================================
@app.post("/api/generate")
async def generate_comment(request: AIRequest, background_tasks: BackgroundTasks):
    add_log(f"📥 Extension gửi yêu cầu nhận xét...", "INFO")
    try:
        # Sử dụng SDK mới của Google (google-genai)
        client = genai.Client(api_key=request.api_key)
        
        response = client.models.generate_content(
            model=request.model,
            contents=request.prompt,
        )
        
        ai_text = response.text.strip()
        
        # Chạy bóc tách HTML ngầm để trả kết quả AI cho người dùng ngay lập tức
        background_tasks.add_task(process_and_save_data, request.raw_html, request.keywords, request.scores, ai_text)
        
        return {"status": "success", "data": ai_text}
    except Exception as e:
        add_log(f"❌ Lỗi AI Gemini: {str(e)}", "ERROR")
        return {"status": "error", "message": str(e)}

@app.get("/api/logs")
async def get_logs(): return {"logs": SYSTEM_LOGS}

@app.get("/", response_class=HTMLResponse)
async def dashboard():
    return """
    <html><head><title>LMS Tracker Dashboard</title><style>
    body { background: #0a0a0a; color: #00ff41; font-family: 'Consolas', monospace; padding: 40px; }
    .terminal { background: #000; border: 1px solid #00ff41; height: 75vh; overflow-y: auto; padding: 20px; box-shadow: 0 0 20px rgba(0,255,65,0.2); }
    .SUCCESS { color: #00ff41; font-weight: bold; } 
    .ERROR { color: #ff3131; } 
    .INFO { color: #00b8ff; }
    h2 { border-bottom: 2px solid #00ff41; display: inline-block; padding-bottom: 5px; }
    </style></head><body>
    <h2>> LMS_PERFORMANCE_TRACKER_SYSTEM</h2>
    <p>Database Status: <span class="SUCCESS">CONNECTED (AIVEN_CLOUD)</span></p>
    <div class="terminal" id="terminal"></div>
    <script>
        setInterval(async () => {
            try {
                const r = await fetch('/api/logs'); const d = await r.json();
                document.getElementById('terminal').innerHTML = d.logs.map(l => `<div>[${l.time}] <span class="${l.status}">>> ${l.msg}</span></div>`).join('');
            } catch(e) {}
        }, 1000);
    </script></body></html>
    """

if __name__ == "__main__":
    uvicorn.run("main:app", host="127.0.0.1", port=8000, reload=True)