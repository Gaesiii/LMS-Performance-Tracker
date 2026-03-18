from main import SessionLocal, ClassRecord, Student, Lesson, Evaluation

def check_database():
    print("Đang kết nối tới Aiven Cloud...")
    db = SessionLocal()
    try:
        # Lấy tất cả các lớp học
        classes = db.query(ClassRecord).all()
        print(f"\n=== TỔNG SỐ LỚP HỌC: {len(classes)} ===")
        for c in classes:
            print(f"- Lớp: {c.class_code}")

        # Lấy tất cả học sinh
        students = db.query(Student).all()
        print(f"\n=== TỔNG SỐ HỌC SINH: {len(students)} ===")
        for s in students:
            print(f"- {s.name} (ID Lớp: {s.class_id})")

        # Lấy 5 bài đánh giá gần nhất
        evals = db.query(Evaluation).order_by(Evaluation.id.desc()).limit(5).all()
        print(f"\n=== 5 BÀI ĐÁNH GIÁ MỚI NHẤT ===")
        for e in evals:
            student_name = db.query(Student).filter(Student.id == e.student_id).first().name
            print(f"Học sinh: {student_name}")
            print(f"Điểm: {e.scores}")
            print(f"Nhận xét AI: {e.ai_comment[:50]}...\n")
            
    except Exception as e:
        print(f"Lỗi truy vấn: {e}")
    finally:
        db.close()

if __name__ == "__main__":
    check_database()