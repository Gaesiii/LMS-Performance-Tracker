import hashlib
import os
import re
import secrets
from datetime import datetime, timedelta, timezone
from typing import Generator, List, Optional

import uvicorn
from bs4 import BeautifulSoup
from dotenv import load_dotenv
from fastapi import BackgroundTasks, Depends, FastAPI, Header, HTTPException, Query, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse
from google import genai
from pydantic import BaseModel, Field
from sqlalchemy import Boolean, Column, DateTime, ForeignKey, Integer, String, Text, create_engine
from sqlalchemy.orm import Session, declarative_base, relationship, sessionmaker


# ==========================================
# 1. Environment + Database setup
# ==========================================
load_dotenv()

raw_db_url = os.getenv("DATABASE_URL", "").strip()
if raw_db_url.startswith("mysql://"):
    DATABASE_URL = raw_db_url.replace("mysql://", "mysql+pymysql://", 1)
elif raw_db_url:
    DATABASE_URL = raw_db_url
else:
    DATABASE_URL = "sqlite:///./lms_tracker.db"

CA_PEM_PATH = os.path.join(os.path.dirname(__file__), "ca.pem")

connect_args = {}
if DATABASE_URL.startswith("sqlite"):
    connect_args["check_same_thread"] = False
elif DATABASE_URL.startswith("mysql") and os.path.exists(CA_PEM_PATH):
    connect_args["ssl"] = {"ca": CA_PEM_PATH}

engine = create_engine(
    DATABASE_URL,
    connect_args=connect_args,
    pool_recycle=3600,
    pool_pre_ping=True,
)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()


# ==========================================
# 2. Database models
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


class UserAccount(Base):
    __tablename__ = "users"

    id = Column(Integer, primary_key=True, index=True)
    user_uid = Column(String(150), unique=True, index=True, nullable=True)
    email = Column(String(180), unique=True, index=True, nullable=True)
    full_name = Column(String(120), default="")
    role = Column(String(20), default="teacher", index=True)  # teacher | sale | admin
    auth_provider = Column(String(20), default="manual")
    password_salt = Column(String(64), nullable=True)
    password_hash = Column(String(256), nullable=True)
    is_active = Column(Boolean, default=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    sessions = relationship("AuthSession", back_populates="user")
    assigned_tasks = relationship(
        "TrialTask",
        foreign_keys="TrialTask.assigned_by_user_id",
        back_populates="assigned_by",
    )
    received_tasks = relationship(
        "TrialTask",
        foreign_keys="TrialTask.teacher_user_id",
        back_populates="teacher",
    )


class AuthSession(Base):
    __tablename__ = "auth_sessions"

    id = Column(Integer, primary_key=True, index=True)
    token = Column(String(220), unique=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), index=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    expires_at = Column(DateTime, index=True)

    user = relationship("UserAccount", back_populates="sessions")


class TrialTask(Base):
    __tablename__ = "trial_tasks"

    id = Column(Integer, primary_key=True, index=True)
    task_uid = Column(String(80), unique=True, index=True)

    teacher_user_id = Column(Integer, ForeignKey("users.id"), index=True)
    assigned_by_user_id = Column(Integer, ForeignKey("users.id"), nullable=True, index=True)

    student_name = Column(String(140), index=True)
    age = Column(Integer, default=0)
    course = Column(String(140))
    notes = Column(Text)
    consultant_info = Column(String(180))
    schedule_date = Column(String(20))
    schedule_time = Column(String(20))
    trial_status = Column(String(20), default="pending")
    absent = Column(Boolean, default=False)
    feedback = Column(Text)
    data_logs = Column(Text)
    source = Column(String(30), default="assigned")
    submitted_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    teacher = relationship(
        "UserAccount",
        foreign_keys=[teacher_user_id],
        back_populates="received_tasks",
    )
    assigned_by = relationship(
        "UserAccount",
        foreign_keys=[assigned_by_user_id],
        back_populates="assigned_tasks",
    )


Base.metadata.create_all(bind=engine)


# ==========================================
# 3. App + utility helpers
# ==========================================
app = FastAPI(title="LMS Performance Tracker API")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

SYSTEM_LOGS = []
SESSION_TTL_HOURS = int(os.getenv("SESSION_TTL_HOURS", "24"))


def utcnow() -> datetime:
    return datetime.utcnow()


def to_iso(dt: Optional[datetime]) -> Optional[str]:
    if not dt:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def parse_iso_datetime(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    text = value.strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone(timezone.utc).replace(tzinfo=None)
    return parsed


def add_log(msg: str, status_text: str = "INFO") -> None:
    now_str = datetime.now().strftime("%H:%M:%S")
    if len(SYSTEM_LOGS) > 60:
        SYSTEM_LOGS.pop()
    SYSTEM_LOGS.insert(0, {"time": now_str, "msg": msg, "status": status_text})
    print(f"[{now_str}] [{status_text}] {msg}")


def get_db() -> Generator[Session, None, None]:
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def hash_password(password: str, salt: str) -> str:
    hashed = hashlib.pbkdf2_hmac(
        "sha256",
        password.encode("utf-8"),
        salt.encode("utf-8"),
        120000,
    )
    return hashed.hex()


def verify_password(password: str, user: UserAccount) -> bool:
    if not user.password_hash or not user.password_salt:
        return False
    compared = hash_password(password, user.password_salt)
    return secrets.compare_digest(compared, user.password_hash)


def extract_bearer_token(authorization: Optional[str]) -> Optional[str]:
    if not authorization:
        return None
    parts = authorization.strip().split(" ", 1)
    if len(parts) == 2 and parts[0].lower() == "bearer" and parts[1].strip():
        return parts[1].strip()
    return None


def get_user_from_token(db: Session, token: Optional[str]) -> Optional[UserAccount]:
    if not token:
        return None

    session_row = db.query(AuthSession).filter(AuthSession.token == token).first()
    if not session_row:
        return None

    if session_row.expires_at < utcnow():
        db.delete(session_row)
        db.commit()
        return None

    return (
        db.query(UserAccount)
        .filter(UserAccount.id == session_row.user_id, UserAccount.is_active.is_(True))
        .first()
    )


def require_current_user(
    authorization: Optional[str] = Header(default=None),
    x_auth_token: Optional[str] = Header(default=None),
    db: Session = Depends(get_db),
) -> UserAccount:
    token = extract_bearer_token(authorization) or (x_auth_token.strip() if x_auth_token else None)
    user = get_user_from_token(db, token)
    if not user:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or expired token",
        )
    return user


def require_role(user: UserAccount, allowed_roles: set) -> None:
    if user.role not in allowed_roles:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Permission denied",
        )


def serialize_user(user: UserAccount) -> dict:
    return {
        "id": user.id,
        "uid": user.user_uid,
        "email": user.email,
        "fullName": user.full_name,
        "role": user.role,
        "provider": user.auth_provider,
        "createdAt": to_iso(user.created_at),
    }


def serialize_task(task: TrialTask) -> dict:
    return {
        "id": task.task_uid,
        "studentName": task.student_name or "",
        "age": task.age or 0,
        "course": task.course or "",
        "notes": task.notes or "",
        "consultantInfo": task.consultant_info or "",
        "schedule": {
            "date": task.schedule_date or "",
            "time": task.schedule_time or "",
        },
        "trialStatus": task.trial_status or "pending",
        "absent": bool(task.absent),
        "feedback": task.feedback or "",
        "dataLogs": task.data_logs or "",
        "source": task.source or "assigned",
        "updatedAt": to_iso(task.updated_at),
        "submittedAt": to_iso(task.submitted_at),
        "teacherUid": task.teacher.user_uid if task.teacher else None,
        "assignedBy": task.assigned_by.full_name if task.assigned_by else None,
    }


def create_session(db: Session, user: UserAccount) -> AuthSession:
    token = secrets.token_urlsafe(40)
    session_row = AuthSession(
        token=token,
        user_id=user.id,
        expires_at=utcnow() + timedelta(hours=SESSION_TTL_HOURS),
    )
    db.add(session_row)
    db.commit()
    db.refresh(session_row)
    return session_row


def find_or_create_teacher(db: Session, teacher_uid: str, full_name: str = "") -> UserAccount:
    uid = (teacher_uid or "").strip()
    if not uid:
        raise HTTPException(status_code=400, detail="teacher_uid is required")

    user = db.query(UserAccount).filter(UserAccount.user_uid == uid).first()
    if user:
        if full_name and not user.full_name:
            user.full_name = full_name
            db.commit()
            db.refresh(user)
        return user

    user = UserAccount(
        user_uid=uid,
        email=None,
        full_name=full_name or uid,
        role="teacher",
        auth_provider="manual",
        is_active=True,
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


def ensure_default_sale_account() -> None:
    default_email = os.getenv("SALE_DEFAULT_EMAIL", "sale@mindx.local").strip().lower()
    default_name = os.getenv("SALE_DEFAULT_NAME", "Default Sale").strip()
    default_password = os.getenv("SALE_DEFAULT_PASSWORD", "").strip()

    if not default_password:
        add_log("SALE_DEFAULT_PASSWORD is empty. Skip default sale bootstrap.", "INFO")
        return

    db = SessionLocal()
    try:
        existing = db.query(UserAccount).filter(UserAccount.email == default_email).first()
        if existing:
            if existing.role != "sale":
                existing.role = "sale"
                db.commit()
                add_log(f"Updated default account role to sale: {default_email}", "SUCCESS")
            return

        salt = secrets.token_hex(16)
        user = UserAccount(
            user_uid=f"sale-{secrets.token_hex(4)}",
            email=default_email,
            full_name=default_name,
            role="sale",
            auth_provider="password",
            password_salt=salt,
            password_hash=hash_password(default_password, salt),
            is_active=True,
        )
        db.add(user)
        db.commit()
        add_log(f"Created default sale account: {default_email}", "SUCCESS")
    except Exception as exc:
        db.rollback()
        add_log(f"Default sale bootstrap failed: {exc}", "ERROR")
    finally:
        db.close()


# ==========================================
# 4. Request models
# ==========================================
class AIRequest(BaseModel):
    prompt: str
    model: str
    api_key: str
    keywords: str
    scores: str
    raw_html: str


class SaleLoginRequest(BaseModel):
    email: str
    password: str


class TeacherTokenRequest(BaseModel):
    teacher_uid: str
    teacher_name: str = ""


class SaleAssignTaskItem(BaseModel):
    teacher_uid: str = Field(min_length=1)
    student_name: str = Field(min_length=1)
    age: int = 0
    course: str = Field(min_length=1)
    notes: str = ""
    consultant_info: str = ""
    schedule_date: str = ""
    schedule_time: str = ""
    source: str = "assigned"
    data_logs: str = ""


class SalePushTasksRequest(BaseModel):
    tasks: List[SaleAssignTaskItem] = Field(default_factory=list)


class ExtensionTaskSchedule(BaseModel):
    date: str = ""
    time: str = ""


class ExtensionTaskItem(BaseModel):
    id: Optional[str] = None
    studentName: str = ""
    age: int = 0
    course: str = ""
    notes: str = ""
    consultantInfo: str = ""
    schedule: ExtensionTaskSchedule = Field(default_factory=ExtensionTaskSchedule)
    trialStatus: str = "pending"
    absent: bool = False
    feedback: str = ""
    dataLogs: str = ""
    source: str = "assigned"
    updatedAt: Optional[str] = None


class ExtensionSubmitRequest(BaseModel):
    userId: str
    authMethod: str = ""
    token: str = ""
    submittedAt: Optional[str] = None
    tasks: List[ExtensionTaskItem] = Field(default_factory=list)


# ==========================================
# 5. Existing AI parsing flow
# ==========================================
def process_and_save_data(raw_html: str, keywords: str, scores_str: str, ai_comment: str) -> None:
    add_log("Parsing LMS HTML and storing evaluation...", "INFO")
    db = SessionLocal()
    try:
        soup = BeautifulSoup(raw_html, "html.parser")

        class_code = "UNKNOWN"
        for tag in soup.find_all("h6", class_="MuiTypography-h6"):
            match = re.search(r"\b([A-Z]{2,4}-[A-Z0-9]+-[A-Z0-9]+)\b", tag.text)
            if match:
                class_code = match.group(1)
                break

        lesson_number = 1
        for div in soup.select(".info-container div"):
            match = re.search(r"#\s*(\d+)", div.text)
            if match:
                lesson_number = int(match.group(1))
                break

        student_name = "Anonymous"
        valid_names = [span.get_text(strip=True) for span in soup.find_all("span", class_="name-display")]
        popup = soup.find("div", role="dialog") or soup
        for h3 in popup.find_all("h3"):
            h3_text = h3.get_text(strip=True)
            for name in valid_names:
                if name in h3_text:
                    student_name = name
                    break
            if student_name != "Anonymous":
                break

        db_class = db.query(ClassRecord).filter(ClassRecord.class_code == class_code).first()
        if not db_class:
            db_class = ClassRecord(class_code=class_code)
            db.add(db_class)
            db.commit()
            db.refresh(db_class)

        db_student = (
            db.query(Student)
            .filter(Student.name == student_name, Student.class_id == db_class.id)
            .first()
        )
        if not db_student:
            db_student = Student(name=student_name, class_id=db_class.id)
            db.add(db_student)
            db.commit()
            db.refresh(db_student)

        db_lesson = (
            db.query(Lesson)
            .filter(Lesson.lesson_number == lesson_number, Lesson.class_id == db_class.id)
            .first()
        )
        if not db_lesson:
            db_lesson = Lesson(lesson_number=lesson_number, class_id=db_class.id)
            db.add(db_lesson)
            db.commit()
            db.refresh(db_lesson)

        db.add(
            Evaluation(
                student_id=db_student.id,
                lesson_id=db_lesson.id,
                keywords=keywords,
                scores=scores_str,
                ai_comment=ai_comment,
            )
        )
        db.commit()
        add_log(f"Evaluation saved: {student_name} | {class_code} | Lesson {lesson_number}", "SUCCESS")
    except Exception as exc:
        db.rollback()
        add_log(f"Database save error: {exc}", "ERROR")
    finally:
        db.close()


# ==========================================
# 6. API endpoints
# ==========================================
@app.on_event("startup")
def bootstrap() -> None:
    ensure_default_sale_account()


@app.post("/api/generate")
async def generate_comment(request: AIRequest, background_tasks: BackgroundTasks):
    add_log("Received AI generation request from extension.", "INFO")
    try:
        client = genai.Client(api_key=request.api_key)
        response = client.models.generate_content(
            model=request.model,
            contents=request.prompt,
        )
        ai_text = (response.text or "").strip()

        background_tasks.add_task(
            process_and_save_data,
            request.raw_html,
            request.keywords,
            request.scores,
            ai_text,
        )
        return {"status": "success", "data": ai_text}
    except Exception as exc:
        add_log(f"Gemini error: {exc}", "ERROR")
        return {"status": "error", "message": str(exc)}


@app.post("/api/auth/sale/login")
def sale_login(payload: SaleLoginRequest, db: Session = Depends(get_db)):
    email = payload.email.strip().lower()
    user = db.query(UserAccount).filter(UserAccount.email == email).first()
    if not user or not user.is_active:
        raise HTTPException(status_code=401, detail="Invalid email or password")
    if user.role not in {"sale", "admin"}:
        raise HTTPException(status_code=403, detail="Only sale/admin accounts can use this endpoint")
    if not verify_password(payload.password, user):
        raise HTTPException(status_code=401, detail="Invalid email or password")

    session_row = create_session(db, user)
    add_log(f"Sale login: {email}", "SUCCESS")
    return {
        "status": "success",
        "data": {
            "token": session_row.token,
            "expiresAt": to_iso(session_row.expires_at),
            "user": serialize_user(user),
        },
    }


@app.post("/api/auth/logout")
def logout(
    authorization: Optional[str] = Header(default=None),
    x_auth_token: Optional[str] = Header(default=None),
    db: Session = Depends(get_db),
):
    token = extract_bearer_token(authorization) or (x_auth_token.strip() if x_auth_token else None)
    if not token:
        raise HTTPException(status_code=400, detail="Missing token")

    session_row = db.query(AuthSession).filter(AuthSession.token == token).first()
    if session_row:
        db.delete(session_row)
        db.commit()
    return {"status": "success"}


@app.get("/api/auth/me")
def auth_me(current_user: UserAccount = Depends(require_current_user)):
    return {"status": "success", "user": serialize_user(current_user)}


@app.post("/api/sale/teachers/token")
def create_teacher_token(
    payload: TeacherTokenRequest,
    current_user: UserAccount = Depends(require_current_user),
    db: Session = Depends(get_db),
):
    require_role(current_user, {"sale", "admin"})
    teacher = find_or_create_teacher(db, payload.teacher_uid, payload.teacher_name)
    session_row = create_session(db, teacher)
    add_log(f"Sale generated teacher token for UID={teacher.user_uid}", "SUCCESS")
    return {
        "status": "success",
        "data": {
            "token": session_row.token,
            "expiresAt": to_iso(session_row.expires_at),
            "teacher": serialize_user(teacher),
        },
    }


@app.post("/api/sale/tasks/push")
def sale_push_tasks(
    payload: SalePushTasksRequest,
    current_user: UserAccount = Depends(require_current_user),
    db: Session = Depends(get_db),
):
    require_role(current_user, {"sale", "admin"})
    if not payload.tasks:
        raise HTTPException(status_code=400, detail="tasks cannot be empty")

    created = []
    for item in payload.tasks:
        teacher = find_or_create_teacher(db, item.teacher_uid)
        task = TrialTask(
            task_uid=f"trial-{secrets.token_hex(8)}",
            teacher_user_id=teacher.id,
            assigned_by_user_id=current_user.id,
            student_name=item.student_name.strip(),
            age=max(item.age or 0, 0),
            course=item.course.strip(),
            notes=item.notes.strip(),
            consultant_info=item.consultant_info.strip() or current_user.full_name or current_user.email or "",
            schedule_date=item.schedule_date.strip(),
            schedule_time=item.schedule_time.strip(),
            trial_status="pending",
            absent=False,
            feedback="",
            data_logs=item.data_logs.strip(),
            source=item.source.strip() or "assigned",
            submitted_at=None,
        )
        db.add(task)
        db.flush()
        created.append(task)

    db.commit()
    response_tasks = [serialize_task(task) for task in created]
    add_log(f"Sale pushed {len(response_tasks)} trial task(s).", "SUCCESS")
    return {"status": "success", "count": len(response_tasks), "tasks": response_tasks}


@app.get("/api/sale/tasks")
def sale_list_tasks(
    teacher_uid: Optional[str] = Query(default=None),
    current_user: UserAccount = Depends(require_current_user),
    db: Session = Depends(get_db),
):
    require_role(current_user, {"sale", "admin"})

    query = db.query(TrialTask).join(UserAccount, TrialTask.teacher_user_id == UserAccount.id)
    if teacher_uid:
        query = query.filter(UserAccount.user_uid == teacher_uid.strip())

    rows = query.order_by(TrialTask.created_at.desc()).limit(500).all()
    return {"status": "success", "count": len(rows), "tasks": [serialize_task(task) for task in rows]}


def validate_extension_token_for_user(
    db: Session,
    maybe_token: str,
    teacher_uid: str,
) -> None:
    if not maybe_token:
        return
    token_user = get_user_from_token(db, maybe_token.strip())
    if not token_user:
        raise HTTPException(status_code=401, detail="Invalid token in request")
    if token_user.role in {"sale", "admin"}:
        return
    if token_user.user_uid != teacher_uid:
        raise HTTPException(status_code=403, detail="Token does not match teacher UID")


@app.get("/api/trial-tasks")
def extension_get_tasks(
    user_id: str = Query(..., alias="userId"),
    token: Optional[str] = Query(default=None),
    db: Session = Depends(get_db),
):
    uid = user_id.strip()
    if not uid:
        raise HTTPException(status_code=400, detail="userId is required")

    validate_extension_token_for_user(db, token or "", uid)
    teacher = find_or_create_teacher(db, uid)
    rows = (
        db.query(TrialTask)
        .filter(TrialTask.teacher_user_id == teacher.id)
        .order_by(TrialTask.created_at.desc())
        .all()
    )
    return {"status": "success", "count": len(rows), "tasks": [serialize_task(task) for task in rows]}


@app.post("/api/trial-tasks/submit")
def extension_submit_tasks(payload: ExtensionSubmitRequest, db: Session = Depends(get_db)):
    uid = payload.userId.strip()
    if not uid:
        raise HTTPException(status_code=400, detail="userId is required")

    validate_extension_token_for_user(db, payload.token.strip(), uid)
    teacher = find_or_create_teacher(db, uid)

    submitted_at = parse_iso_datetime(payload.submittedAt) or utcnow()
    created_count = 0
    updated_count = 0

    for item in payload.tasks:
        task_uid = (item.id or "").strip() or f"trial-{secrets.token_hex(8)}"
        row = (
            db.query(TrialTask)
            .filter(TrialTask.task_uid == task_uid, TrialTask.teacher_user_id == teacher.id)
            .first()
        )

        if not row:
            row = TrialTask(
                task_uid=task_uid,
                teacher_user_id=teacher.id,
                assigned_by_user_id=None,
                source=(item.source or "unexpected").strip() or "unexpected",
            )
            db.add(row)
            created_count += 1
        else:
            updated_count += 1

        normalized_status = (item.trialStatus or "pending").strip().lower()
        if normalized_status not in {"pending", "pass", "fail"}:
            normalized_status = "pending"

        row.student_name = (item.studentName or "").strip()
        row.age = max(item.age or 0, 0)
        row.course = (item.course or "").strip()
        row.notes = (item.notes or "").strip()
        row.consultant_info = (item.consultantInfo or "").strip()
        row.schedule_date = (item.schedule.date or "").strip()
        row.schedule_time = (item.schedule.time or "").strip()
        row.trial_status = normalized_status
        row.absent = bool(item.absent)
        row.feedback = (item.feedback or "").strip()
        row.data_logs = (item.dataLogs or "").strip()
        row.source = (item.source or row.source or "assigned").strip()
        row.submitted_at = submitted_at
        row.updated_at = parse_iso_datetime(item.updatedAt) or utcnow()

    db.commit()
    add_log(f"Teacher [{uid}] submitted {len(payload.tasks)} task(s).", "SUCCESS")
    return {
        "status": "success",
        "count": len(payload.tasks),
        "created": created_count,
        "updated": updated_count,
        "submittedAt": to_iso(submitted_at),
    }


@app.get("/api/logs")
async def get_logs():
    return {"logs": SYSTEM_LOGS}


@app.get("/", response_class=HTMLResponse)
async def dashboard():
    return """
    <html>
      <head>
        <title>LMS Tracker Dashboard</title>
        <style>
          body { background: #0a0a0a; color: #00ff41; font-family: Consolas, monospace; padding: 32px; }
          .terminal { background: #000; border: 1px solid #00ff41; height: 70vh; overflow-y: auto; padding: 16px; }
          .SUCCESS { color: #00ff41; font-weight: bold; }
          .ERROR { color: #ff3131; }
          .INFO { color: #00b8ff; }
          h2 { border-bottom: 2px solid #00ff41; display: inline-block; padding-bottom: 6px; margin-top: 0; }
          .tips { margin: 14px 0 20px; color: #8be9fd; line-height: 1.4; }
        </style>
      </head>
      <body>
        <h2>LMS PERFORMANCE TRACKER API</h2>
        <div class="tips">
          Endpoints:<br/>
          - POST /api/auth/sale/login<br/>
          - POST /api/sale/tasks/push<br/>
          - GET /api/trial-tasks?userId=...<br/>
          - POST /api/trial-tasks/submit
        </div>
        <div class="terminal" id="terminal"></div>
        <script>
          setInterval(async () => {
            try {
              const r = await fetch('/api/logs');
              const d = await r.json();
              document.getElementById('terminal').innerHTML = d.logs.map(
                l => `<div>[${l.time}] <span class="${l.status}">>> ${l.msg}</span></div>`
              ).join('');
            } catch (e) {}
          }, 1000);
        </script>
      </body>
    </html>
    """


if __name__ == "__main__":
    port = int(os.getenv("PORT", "8000"))
    uvicorn.run("main:app", host="0.0.0.0", port=port)
