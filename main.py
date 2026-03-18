from fastapi import FastAPI, BackgroundTasks
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse
from pydantic import BaseModel
import google.generativeai as genai
from datetime import datetime
from bs4 import BeautifulSoup
import re
import uvicorn

# --- THƯ VIỆN DATABASE MYSQL ---
from sqlalchemy import create_engine, Column, Integer, String, Text, DateTime, ForeignKey
from sqlalchemy.orm import declarative_base, sessionmaker, relationship

# ==========================================
# 1. CẤU HÌNH DATABASE MYSQL
# ==========================================
SQLALCHEMY_DATABASE_URL = "mysql+pymysql://root:123456@localhost:3306/mindx_ai_db"
engine = create_engine(SQLALCHEMY_DATABASE_URL)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()

# ==========================================
# 2. KHAI BÁO BẢNG (RELATIONAL SCHEMA)
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

Base.metadata.create_all(bind=engine)

# ==========================================
# 3. FASTAPI & HỆ THỐNG LOGS
# ==========================================
app = FastAPI(title="Local AI Hub MySQL")
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

# ==========================================
# 4. HÀM CHẠY NGẦM: BÓC TÁCH BS4 & LƯU DB
# ==========================================
def process_and_save_data(raw_html: str, keywords: str, scores_str: str, ai_comment: str):
    add_log("⚙️ [Chạy ngầm] Đang dùng BS4 bóc tách HTML...", "INFO")
    
    class_code = None
    lesson_number = None
    student_name = "Chưa nhận diện được tên"
    
    try:
        soup = BeautifulSoup(raw_html, 'html.parser')
        
        # --- A. TÌM MÃ LỚP ---
        h6_tags = soup.find_all('h6', class_='MuiTypography-h6')
        for tag in h6_tags:
            match = re.search(r'\b([A-Z]{2,4}-[A-Z0-9]+-[A-Z0-9]+)\b', tag.text)
            if match:
                class_code = match.group(1)
                break
                
        # --- B. TÌM SỐ BUỔI ---
        info_divs = soup.select('.info-container div')
        for div in info_divs:
            match = re.search(r'#\s*(\d+)', div.text)
            if match:
                lesson_number = int(match.group(1))
                break

        # --- C. TÌM TÊN BẰNG THUẬT TOÁN ĐỐI CHIẾU CHÉO (H3 & NAME-DISPLAY) ---
        
        # Bước 1: Quét danh sách toàn bộ Tên hợp lệ từ thẻ span.name-display ở trang nền
        valid_names = [span.get_text(strip=True) for span in soup.find_all('span', class_='name-display') if span.get_text(strip=True)]
        
        if valid_names:
            # Bước 2: Tìm vùng Popup Form (role='dialog' hoặc quét toàn thân nếu không rõ class)
            popup_area = soup.find('div', role='dialog') or soup
            
            # Bước 3: Tìm tất cả thẻ H3 trong vùng này
            h3_tags = popup_area.find_all('h3')
            
            found_name = None
            for h3 in h3_tags:
                h3_text = h3.get_text(strip=True)
                
                # Dò xem tên nào trong danh sách hợp lệ nằm bên trong câu của thẻ H3
                for name in valid_names:
                    if name in h3_text:
                        found_name = name
                        break # Dừng dò tên
                
                if found_name:
                    break # Dừng quét H3
                    
            if found_name:
                student_name = found_name
                add_log(f"🎯 Đối chiếu chéo thành công Tên học viên: {student_name}", "SUCCESS")
            else:
                add_log("⚠️ Không tìm thấy tên nào khớp giữa danh sách (span) và Popup (H3)!", "WARNING")
        else:
            add_log("⚠️ Không quét được danh sách tên nền (span.name-display)!", "WARNING")

    except Exception as e:
        add_log(f"❌ Lỗi BeautifulSoup: {str(e)}", "ERROR")
        return

    if not class_code or not lesson_number:
        add_log("❌ Không bắt được Mã lớp hoặc Buổi học. Bỏ qua lưu Database!", "ERROR")
        return

    # --- D. XỬ LÝ LƯU DATABASE MYSQL ---
    db = SessionLocal()
    try:
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
            student_id=db_student.id,
            lesson_id=db_lesson.id,
            keywords=keywords,
            scores=scores_str, 
            ai_comment=ai_comment
        )
        db.add(new_eval)
        db.commit()
        
        add_log(f"💾 [MySQL] Đã lưu! | Lớp: {class_code} | Buổi: {lesson_number} | Tên: {student_name}", "SUCCESS")
    except Exception as e:
        db.rollback()
        add_log(f"❌ Lỗi lưu MySQL: {str(e)}", "ERROR")
    finally:
        db.close()

# ==========================================
# 5. API ENDPOINT CHÍNH (GỌI AI & KÍCH HOẠT LƯU NGẦM)
# ==========================================
@app.post("/api/generate")
async def generate_comment(request: AIRequest, background_tasks: BackgroundTasks):
    add_log(f"📥 REQUEST ĐẾN: Nhận {len(request.raw_html)} ký tự HTML từ Extension...", "INFO")
    add_log(f"   👉 Từ khóa: {request.keywords}", "INFO")
    add_log(f"   👉 Barem điểm: {request.scores}", "INFO")
    
    try:
        genai.configure(api_key=request.api_key)
        model = genai.GenerativeModel(request.model)
        
        add_log(f"🧠 Đang gửi Prompt cho {request.model}...", "INFO")
        response = model.generate_content(request.prompt)
        ai_result_text = response.text.strip()
        add_log("✅ AI đã sinh nhận xét siêu tốc!", "SUCCESS")
        
        # Đẩy công việc bóc tách HTML và lưu MySQL ra luồng Background
        background_tasks.add_task(
            process_and_save_data, 
            request.raw_html, 
            request.keywords, 
            request.scores,
            ai_result_text
        )
        
        return {"status": "success", "data": ai_result_text}
        
    except Exception as e:
        add_log(f"❌ Lỗi API Gemini: {str(e)}", "ERROR")
        return {"status": "error", "message": str(e)}

@app.get("/api/logs")
async def get_logs(): return {"logs": SYSTEM_LOGS}

# ==========================================
# 6. GIAO DIỆN TERMINAL TRÊN TRÌNH DUYỆT
# ==========================================
@app.get("/", response_class=HTMLResponse)
async def dashboard():
    return """
    <!DOCTYPE html>
    <html lang="vi">
    <head>
        <meta charset="UTF-8">
        <title>Trạm Giám Sát AI</title>
        <style>
            body { background: #121212; color: #00ff00; font-family: 'Courier New', monospace; padding: 20px; }
            h2 { color: #fff; border-bottom: 1px solid #333; padding-bottom: 10px;}
            .log-box { background: #000; padding: 20px; border-radius: 5px; border: 1px solid #333; height: 70vh; overflow-y: auto;}
            .log-time { color: #888; margin-right: 10px; }
            .log-INFO { color: #00d8ff; }
            .log-SUCCESS { color: #00ff00; font-weight: bold;}
            .log-ERROR { color: #ff5555; font-weight: bold;}
            .log-row { margin-bottom: 8px; line-height: 1.4;}
        </style>
    </head>
    <body>
        <h2>🖥️ Trạm Cào Data & AI Dataset Hub</h2>
        <div style="margin-bottom: 10px; color: #888;">Trạng thái Database: <i>Sẵn sàng (MySQL Relational)</i></div>
        <div class="log-box" id="term"><div style="color:#888;">> Hệ thống đang lắng nghe...</div></div>
        <script>
            async function fetchLogs() {
                try {
                    const res = await fetch('/api/logs'); const data = await res.json();
                    if(data.logs.length > 0) {
                        document.getElementById('term').innerHTML = data.logs.map(l => `<div class="log-row"><span class="log-time">[${l.time}]</span><span class="log-${l.status}">> ${l.msg}</span></div>`).join('');
                    }
                } catch(e) {}
            }
            setInterval(fetchLogs, 1000);
        </script>
    </body>
    </html>
    """

if __name__ == "__main__":
    uvicorn.run("main:app", host="127.0.0.1", port=8000, reload=True)