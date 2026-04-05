# LMS Performance Tracker (Server)

## Run
```bash
pip install -r requirements.txt
uvicorn main:app --reload
```

## Required env
- `DATABASE_URL` (MySQL/Aiven or fallback to local SQLite if missing)
- `SALE_DEFAULT_EMAIL` (optional, default: `sale@mindx.local`)
- `SALE_DEFAULT_NAME` (optional, default: `Default Sale`)
- `SALE_DEFAULT_PASSWORD` (required if you want bootstrap sale login account)
- `SESSION_TTL_HOURS` (optional, default: `24`)

## Core APIs

### 1) Sale login
`POST /api/auth/sale/login`
```json
{
  "email": "sale@mindx.local",
  "password": "your_password"
}
```

### 2) Sale push trial tasks to teachers
`POST /api/sale/tasks/push`

Headers:
- `Authorization: Bearer <sale_token>`

Body:
```json
{
  "tasks": [
    {
      "teacher_uid": "teacher_001",
      "student_name": "Nguyen Minh Anh",
      "age": 10,
      "course": "Scratch SB",
      "notes": "First trial class",
      "consultant_info": "Sale A",
      "schedule_date": "2026-04-06",
      "schedule_time": "18:30",
      "source": "assigned",
      "data_logs": "pushed from sale dashboard"
    }
  ]
}
```

### 3) Extension fetch assigned tasks
`GET /api/trial-tasks?userId=<teacher_uid>&token=<optional_teacher_token>`

### 4) Extension submit trial result
`POST /api/trial-tasks/submit`
```json
{
  "userId": "teacher_001",
  "authMethod": "manual",
  "token": "",
  "submittedAt": "2026-04-06T10:30:00Z",
  "tasks": [
    {
      "id": "trial-abc123",
      "studentName": "Nguyen Minh Anh",
      "age": 10,
      "course": "Scratch SB",
      "notes": "First trial class",
      "consultantInfo": "Sale A",
      "schedule": {
        "date": "2026-04-06",
        "time": "18:30"
      },
      "trialStatus": "pass",
      "absent": false,
      "feedback": "Good communication and logic",
      "dataLogs": "submitted from extension popup",
      "source": "assigned",
      "updatedAt": "2026-04-06T10:29:00Z"
    }
  ]
}
```

### 5) Optional teacher token generation (by sale)
`POST /api/sale/teachers/token`

Headers:
- `Authorization: Bearer <sale_token>`

Body:
```json
{
  "teacher_uid": "teacher_001",
  "teacher_name": "Teacher Minh"
}
```
